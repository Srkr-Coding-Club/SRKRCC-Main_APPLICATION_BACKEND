"""The Club ID registry: history of every change, reservations, retirement, the admin's controls and the counters."""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import ClubIDSequence, ClubIdEvent, ClubIdReservation
from apps.accounts.services import club_id_registry as registry
from apps.accounts.services.club_id_service import ClubIDService

User = get_user_model()
BASE = '/api/auth/club-ids'


def person(email, **extra):
    return User.objects.create_user(username=email.split('@')[0] + str(User.objects.count()), email=email, password='Str0ng!Passw0rd', **extra)


def api(user=None):
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


class RegistryCase(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = person('boss@example.org', role='ADMIN')
        self.lead = person('lead@example.org', role='CLUB_LEAD')
        self.sita = person('sita@example.org', first_name='Sita', last_name='Rao')
        self.omar = person('omar@example.org', first_name='Omar')
        self.holder = person('holder@example.org', first_name='Hana', role='AFFILIATE', club_id='25SCC010')

    def post(self, path, user=None, **body):
        return api(user or self.admin).post(f'{BASE}/{path}', body, format='json')

    def kinds(self, club_id):
        return list(ClubIdEvent.objects.filter(club_id=club_id).order_by('id').values_list('kind', flat=True))


class History(RegistryCase):
    def test_an_id_given_at_sign_up_is_recorded(self):
        self.assertEqual(self.kinds('25SCC010'), ['ASSIGNED'])

    def test_an_admin_editing_the_id_through_the_member_form_is_recorded_with_who_did_it(self):
        reply = api(self.admin).patch(f'/api/auth/users/{self.sita.pk}/', {'club_id': '25SCC050'}, format='json')
        self.assertEqual(reply.status_code, 200, reply.data)
        event = ClubIdEvent.objects.get(club_id='25SCC050')
        self.assertEqual((event.kind, event.user_email), ('ASSIGNED', 'sita@example.org'))

    def test_saving_a_person_without_touching_the_id_writes_nothing(self):
        before = ClubIdEvent.objects.count()
        self.holder.first_name = 'Hannah'
        self.holder.save()
        self.sita.save(update_fields=['last_login'])
        self.assertEqual(ClubIdEvent.objects.count(), before)

    def test_the_events_can_be_listed_and_filtered(self):
        registry.assign(self.admin, self.sita.pk, '25SCC051', reason='old record')
        rows = api(self.lead).get(f'{BASE}/events/', {'club_id': '25SCC051'}).data['results']
        self.assertEqual([(row['kind'], row['actor_email'], row['reason']) for row in rows], [('ASSIGNED', 'boss@example.org', 'old record')])
        self.assertEqual(api(self.lead).get(f'{BASE}/events/', {'kind': 'CLEARED'}).data['count'], 0)
        self.assertEqual(api(self.sita).get(f'{BASE}/events/').status_code, 403)


class Allocating(RegistryCase):
    def test_gives_the_next_number_from_the_counter_and_records_it(self):
        reply = self.post('allocate/', user_id=self.sita.pk, reason='Joined at the stall', year=2026)
        self.assertEqual(reply.status_code, 200, reply.data)
        self.sita.refresh_from_db()
        self.assertEqual(self.sita.club_id, '26SCC001')
        self.assertEqual(self.kinds('26SCC001'), ['ISSUED'])

    def test_skips_numbers_that_are_held_reserved_or_retired(self):
        person('x@example.org', club_id='26SCC001')
        registry.reserve(self.admin, email='res@example.org', club_id='26SCC002', reason='for Res')
        retired = person('r@example.org', club_id='26SCC003')
        registry.clear(self.admin, retired.pk, reason='left')
        self.post('allocate/', user_id=self.sita.pk, reason='next', year=2026)
        self.sita.refresh_from_db()
        self.assertEqual(self.sita.club_id, '26SCC004')

    def test_refuses_someone_who_already_has_one_and_needs_a_reason(self):
        self.assertEqual(self.post('allocate/', user_id=self.holder.pk, reason='x').data['code'], 'ALREADY_HAS_ID')
        self.assertEqual(self.post('allocate/', user_id=self.sita.pk, reason='  ').data['code'], 'REASON_REQUIRED')
        self.assertEqual(self.post('allocate/', user_id=999999, reason='x').status_code, 404)


class Assigning(RegistryCase):
    def test_gives_a_particular_id_in_its_canonical_form(self):
        self.assertEqual(self.post('assign/', user_id=self.sita.pk, club_id='21cca041', reason='old member').status_code, 200)
        self.sita.refresh_from_db()
        self.assertEqual(self.sita.club_id, '21CCA041')

    def test_refuses_an_id_somebody_holds_or_that_is_reserved_for_another_or_malformed(self):
        held = self.post('assign/', user_id=self.sita.pk, club_id='25SCC010', reason='x')
        self.assertEqual((held.status_code, held.data['code']), (409, 'CLUB_ID_UNAVAILABLE'))
        registry.reserve(self.admin, email='res@example.org', club_id='25SCC060', reason='x')
        self.assertEqual(self.post('assign/', user_id=self.sita.pk, club_id='25SCC060', reason='x').data['code'], 'CLUB_ID_UNAVAILABLE')
        self.assertEqual(self.post('assign/', user_id=self.sita.pk, club_id='nonsense', reason='x').data['code'], 'INVALID_CLUB_ID')

    def test_a_person_may_be_given_the_id_reserved_for_their_own_email(self):
        registry.reserve(self.admin, email='sita@example.org', club_id='25SCC061', reason='returning member')
        self.assertEqual(self.post('assign/', user_id=self.sita.pk, club_id='25SCC061', reason='ok').status_code, 200)
        self.assertEqual(ClubIdReservation.objects.get(club_id='25SCC061').status, 'CLAIMED')
        self.assertIn('CLAIMED', self.kinds('25SCC061'))

    def test_someone_who_has_an_id_must_have_it_cleared_or_moved_first(self):
        self.assertEqual(self.post('assign/', user_id=self.holder.pk, club_id='25SCC099', reason='x').data['code'], 'ALREADY_HAS_ID')


class Clearing(RegistryCase):
    def test_clearing_retires_the_id_and_demotes_an_affiliate(self):
        reply = self.post('clear/', user_id=self.holder.pk, reason='Left the club', confirm='25scc010')
        self.assertEqual(reply.status_code, 200, reply.data)
        self.holder.refresh_from_db()
        self.assertEqual((self.holder.club_id, self.holder.role), (None, 'NON_AFFILIATE'))
        self.assertTrue(registry.is_retired('25SCC010'))
        self.assertEqual(self.kinds('25SCC010'), ['ASSIGNED', 'CLEARED'])

    def test_a_retired_id_is_not_given_again_unless_an_admin_says_so(self):
        self.post('clear/', user_id=self.holder.pk, reason='left', confirm='25SCC010')
        refused = self.post('assign/', user_id=self.sita.pk, club_id='25SCC010', reason='x')
        self.assertEqual(refused.data['code'], 'CLUB_ID_UNAVAILABLE')
        self.assertEqual(self.post('assign/', user_id=self.sita.pk, club_id='25SCC010', reason='Clerical error undone', allow_retired=True).status_code, 200)

    def test_needs_the_id_typed_again(self):
        self.assertEqual(self.post('clear/', user_id=self.holder.pk, reason='x', confirm='').data['code'], 'CONFIRM_MISMATCH')
        self.assertEqual(self.post('clear/', user_id=self.holder.pk, reason='x', confirm='25SCC011').data['code'], 'CONFIRM_MISMATCH')
        self.holder.refresh_from_db()
        self.assertEqual(self.holder.club_id, '25SCC010')

    def test_a_person_without_an_id_cannot_be_cleared(self):
        self.assertEqual(self.post('clear/', user_id=self.sita.pk, reason='x', confirm='25SCC010').data['code'], 'NO_CLUB_ID')


class Moving(RegistryCase):
    def test_moves_an_id_between_people_and_records_both_sides(self):
        reply = self.post('reassign/', club_id='25SCC010', to_user_id=self.sita.pk, reason='Was typed under the wrong person', confirm='25SCC010')
        self.assertEqual(reply.status_code, 200, reply.data)
        self.assertTrue(reply.data['holder_demoted'])
        self.holder.refresh_from_db()
        self.sita.refresh_from_db()
        self.assertEqual((self.holder.club_id, self.holder.role, self.sita.club_id), (None, 'NON_AFFILIATE', '25SCC010'))
        self.assertEqual(self.kinds('25SCC010'), ['ASSIGNED', 'REASSIGNED_OUT', 'REASSIGNED_IN'])
        self.assertFalse(registry.is_retired('25SCC010'))

    def test_refuses_the_same_person_a_person_with_an_id_a_wrong_confirmation_and_an_unheld_id(self):
        self.assertEqual(self.post('reassign/', club_id='25SCC010', to_user_id=self.holder.pk, reason='x', confirm='25SCC010').data['code'], 'SAME_PERSON')
        other = person('other@example.org', club_id='25SCC020')
        self.assertEqual(self.post('reassign/', club_id='25SCC010', to_user_id=other.pk, reason='x', confirm='25SCC010').data['code'], 'ALREADY_HAS_ID')
        self.assertEqual(self.post('reassign/', club_id='25SCC010', to_user_id=self.sita.pk, reason='x', confirm='nope').data['code'], 'CONFIRM_MISMATCH')
        self.assertEqual(self.post('reassign/', club_id='25SCC777', to_user_id=self.sita.pk, reason='x', confirm='25SCC777').status_code, 404)


class Reserving(RegistryCase):
    def test_reserves_the_next_number_for_an_email_and_the_counter_moves_on(self):
        reply = self.post('reservations/', email='New.Person@Example.org', reason='Past member', year=2026)
        self.assertEqual(reply.status_code, 201, reply.data)
        self.assertEqual((reply.data['club_id'], reply.data['email'], reply.data['status']), ('26SCC001', 'new.person@example.org', 'RESERVED'))
        self.post('allocate/', user_id=self.sita.pk, reason='next', year=2026)
        self.sita.refresh_from_db()
        self.assertEqual(self.sita.club_id, '26SCC002')

    def test_reserves_a_particular_id_and_refuses_duplicates(self):
        self.assertEqual(self.post('reservations/', email='a@example.org', club_id='21CCA041', reason='old').status_code, 201)
        self.assertEqual(self.post('reservations/', email='b@example.org', club_id='21CCA041', reason='x').data['code'], 'CLUB_ID_UNAVAILABLE')
        self.assertEqual(self.post('reservations/', email='a@example.org', reason='again').data['code'], 'ALREADY_RESERVED')
        self.assertEqual(self.post('reservations/', email='holder@example.org', reason='x').data['code'], 'ALREADY_HAS_ID')
        self.assertEqual(self.post('reservations/', reason='').data['code'], 'REASON_REQUIRED')

    def test_the_reserved_person_gets_it_when_they_register_with_that_email(self):
        self.post('reservations/', email='returning@example.org', club_id='24SCC111', reason='Past member')
        reply = api().post('/api/auth/register/', {
            'username': 'returning', 'email': 'returning@example.org', 'password': 'StrongPass123!', 'first_name': 'Re', 'last_name': 'Turning',
            'roll_number': '22B91A0111', 'branch': 'CSE', 'year': 3,
        }, format='json')
        self.assertEqual(reply.status_code, 201, reply.data)
        self.assertEqual(User.objects.get(email='returning@example.org').club_id, '24SCC111')
        self.assertEqual(ClubIdReservation.objects.get(club_id='24SCC111').status, 'CLAIMED')

    def test_another_person_cannot_register_or_be_edited_into_a_reserved_id(self):
        self.post('reservations/', email='returning@example.org', club_id='24SCC112', reason='x')
        reply = api().post('/api/auth/register/', {
            'username': 'thief', 'email': 'thief@example.org', 'password': 'StrongPass123!', 'first_name': 'Thief', 'last_name': 'Person',
            'roll_number': '22B91A0112', 'branch': 'CSE', 'year': 3, 'club_id': '24SCC112',
        }, format='json')
        self.assertEqual(reply.status_code, 400)
        self.assertIn('club_id', reply.data)
        patched = api(self.admin).patch(f'/api/auth/users/{self.sita.pk}/', {'club_id': '24SCC112'}, format='json')
        self.assertEqual(patched.status_code, 400)

    def test_releasing_returns_the_id_to_the_pool(self):
        reservation = self.post('reservations/', email='a@example.org', club_id='24SCC113', reason='x').data
        self.assertEqual(self.post(f'reservations/{reservation["id"]}/release/').data['status'], 'RELEASED')
        self.assertEqual(self.post('assign/', user_id=self.sita.pk, club_id='24SCC113', reason='free now').status_code, 200)
        self.assertEqual(self.post(f'reservations/{reservation["id"]}/release/').data['code'], 'NOT_ACTIVE')

    def test_the_list_shows_reservations_and_their_state(self):
        self.post('reservations/', email='a@example.org', club_id='24SCC114', reason='x')
        rows = api(self.lead).get(f'{BASE}/reservations/', {'status': 'RESERVED'}).data['results']
        self.assertEqual([(row['club_id'], row['email']) for row in rows], [('24SCC114', 'a@example.org')])


class Counters(RegistryCase):
    def test_moves_forward_only_and_never_onto_a_number_in_use(self):
        ClubIDService.allocate_next_club_id(year=2026)
        self.assertEqual(self.post('counters/', year=2026, next_sequence=50, reason='Skip to 50 for the new batch').status_code, 200)
        self.assertEqual(ClubIDSequence.objects.get(year=2026).next_sequence, 50)
        self.assertEqual(self.post('counters/', year=2026, next_sequence=20, reason='back').data['code'], 'NOT_FORWARD')
        person('late@example.org', club_id='26SCC080')
        self.assertEqual(self.post('counters/', year=2026, next_sequence=60, reason='x').data['code'], 'IN_USE')
        self.assertEqual(self.post('counters/', year=2026, next_sequence=81, reason='ok').status_code, 200)
        self.assertEqual(self.kinds('26SCC050')[-1:], ['COUNTER_SET'])

    def test_only_the_club_counter_and_sensible_values(self):
        self.assertEqual(self.post('counters/', prefix='CCA', year=2021, next_sequence=50, reason='x').data['code'], 'INVALID')
        self.assertEqual(self.post('counters/', year='abc', next_sequence=5, reason='x').data['code'], 'INVALID')
        self.assertEqual(self.post('counters/', year=2026, next_sequence=5, reason='').data['code'], 'REASON_REQUIRED')

    def test_this_years_counter_is_listed_even_before_any_id_of_the_year_exists(self):
        year = timezone.now().year
        row = next(item for item in api(self.lead).get(f'{BASE}/overview/').data['counters'] if (item['prefix'], item['year']) == ('SCC', year))
        self.assertEqual((row['in_use'], row['next_sequence'], row['has_counter'], row['editable']), (0, 1, False, True))
        self.assertEqual(self.post('counters/', year=year, next_sequence=40, reason='new year').status_code, 200)

    def test_the_overview_counts_what_is_in_use_and_where_the_gaps_are(self):
        person('a@example.org', club_id='26SCC001')
        person('b@example.org', club_id='26SCC004')
        data = api(self.lead).get(f'{BASE}/overview/').data
        row = next(item for item in data['counters'] if (item['prefix'], item['year']) == ('SCC', 2026))
        self.assertEqual((row['in_use'], row['highest'], row['gaps'], row['next_id']), (2, 4, 2, '26SCC005'))
        self.assertEqual(data['totals']['holders'], 3)


class Lookup(RegistryCase):
    def test_an_id_shows_its_holder_and_history_and_a_free_id_shows_nothing(self):
        data = api(self.lead).get(f'{BASE}/lookup/', {'q': '25scc010'}).data
        self.assertEqual((data['kind'], data['holder']['email'], data['retired']), ('id', 'holder@example.org', False))
        self.assertEqual([event['kind'] for event in data['events']], ['ASSIGNED'])
        free = api(self.lead).get(f'{BASE}/lookup/', {'q': '25SCC999'}).data
        self.assertEqual((free['holder'], free['reservation'], free['events']), (None, None, []))

    def test_a_person_search_returns_people_and_their_reservations(self):
        self.post('reservations/', email='sitaram@example.org', reason='x', club_id='24SCC120')
        data = api(self.lead).get(f'{BASE}/lookup/', {'q': 'sita'}).data
        self.assertEqual(data['kind'], 'person')
        self.assertEqual([person_['email'] for person_ in data['people']], ['sita@example.org'])
        self.assertEqual([row['email'] for row in data['reservations']], ['sitaram@example.org'])

    def test_an_empty_search_is_refused(self):
        self.assertEqual(api(self.lead).get(f'{BASE}/lookup/', {'q': ' '}).data['code'], 'EMPTY')


class Permissions(RegistryCase):
    def test_a_club_lead_may_read_but_not_change_anything(self):
        for path, body in [('allocate/', {'user_id': self.sita.pk, 'reason': 'x'}), ('assign/', {'user_id': self.sita.pk, 'club_id': '25SCC090', 'reason': 'x'}),
                           ('clear/', {'user_id': self.holder.pk, 'reason': 'x', 'confirm': '25SCC010'}), ('reservations/', {'email': 'a@b.org', 'reason': 'x'}),
                           ('counters/', {'year': 2026, 'next_sequence': 9, 'reason': 'x'})]:
            self.assertEqual(self.post(path, user=self.lead, **body).status_code, 403, path)
        self.assertEqual(api(self.lead).get(f'{BASE}/overview/').status_code, 200)

    def test_participants_and_visitors_get_nothing(self):
        for path in ('overview/', 'lookup/?q=a', 'events/', 'reservations/'):
            self.assertEqual(api(self.sita).get(f'{BASE}/{path}').status_code, 403, path)
            self.assertEqual(api().get(f'{BASE}/{path}').status_code, 401, path)

    def test_every_change_is_also_in_the_audit_log(self):
        from apps.audit.models import AuditLog
        self.post('allocate/', user_id=self.sita.pk, reason='x', year=2026)
        self.assertTrue(AuditLog.objects.filter(action='Club ID Allocated').exists())
