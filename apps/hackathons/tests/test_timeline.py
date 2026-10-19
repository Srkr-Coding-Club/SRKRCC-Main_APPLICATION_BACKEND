from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.hackathons.models import HackathonStage
from apps.hackathons.services import RoundService, StageService

from .helpers import make_hackathon, make_user


class TimelineAdminTests(TestCase):
    def setUp(self):
        self.admin = make_user(role='ADMIN')
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.hack = make_hackathon(min_team_size=1)
        self.base = f'/api/hackathons/{self.hack.slug}/timeline/'
        self.now = timezone.now()

    def _create(self, **overrides):
        body = {'title': 'Prelims', 'description': 'Pitch to the panel', **overrides}
        return self.client.post(self.base, body, format='json')

    def test_admin_creates_a_fully_customised_stage(self):
        response = self._create(
            starts_at=self.now.isoformat(), ends_at=(self.now + timezone.timedelta(hours=3)).isoformat(),
            badge='Online', location='Google Meet', tone='GREEN', marker='ICON', icon='MIC',
            link_label='Join meeting', link_url='https://meet.example.com/abc',
        )
        self.assertEqual(response.status_code, 201, response.data)
        for field, value in {'badge': 'Online', 'location': 'Google Meet', 'tone': 'GREEN', 'marker': 'ICON', 'icon': 'MIC'}.items():
            self.assertEqual(response.data[field], value)
        self.assertEqual(response.data['is_visible'], True)

    def test_new_stages_go_to_the_end(self):
        orders = [self._create(title=name).data['order'] for name in ('A', 'B', 'C')]
        self.assertEqual(orders, [1, 2, 3])

    def test_validation(self):
        later, earlier = self.now + timezone.timedelta(hours=1), self.now
        for payload in (
            {'title': '   '},
            {'title': 'T', 'starts_at': later.isoformat(), 'ends_at': earlier.isoformat()},
            {'title': 'T', 'tone': 'PINK'},
            {'title': 'T', 'marker': 'STAR'},
            {'title': 'T', 'icon': 'DOG'},
            {'title': 'T', 'link_url': 'https://x.io'},
            {'title': 'T', 'link_label': 'Go'},
            {'title': 'T', 'link_label': 'Go', 'link_url': 'ftp://x.io'},
            {'title': 'T', 'badge': 'x' * 41},
            {'title': 'T', 'description': 'x' * 5001},
            {'title': 'x' * 151},
        ):
            self.assertEqual(self.client.post(self.base, payload, format='json').status_code, 400, payload)

    def test_edit_and_delete(self):
        stage_id = self._create().data['id']
        patched = self.client.patch(f'{self.base}{stage_id}/', {'title': 'Prelims 2', 'is_visible': False}, format='json')
        self.assertEqual((patched.data['title'], patched.data['is_visible']), ('Prelims 2', False))
        self.assertEqual(self.client.delete(f'{self.base}{stage_id}/').status_code, 200)
        self.assertFalse(HackathonStage.objects.exists())

    def test_reorder_is_atomic_and_exact(self):
        ids = [self._create(title=name).data['id'] for name in ('A', 'B', 'C')]
        response = self.client.post(f'{self.base}reorder/', {'ids': list(reversed(ids))}, format='json')
        self.assertEqual([stage['id'] for stage in response.data], list(reversed(ids)))
        self.assertEqual([stage['order'] for stage in response.data], [1, 2, 3])
        for bad in ([ids[0]], ids + [9999], [ids[0], ids[0], ids[1]]):
            self.assertEqual(self.client.post(f'{self.base}reorder/', {'ids': bad}, format='json').data['code'], 'REORDER_MISMATCH')

    def test_import_rounds_creates_stages_once(self):
        RoundService.create_round(self.hack, self.admin, name='R1', description='first', starts_at=self.now, populate=False)
        RoundService.create_round(self.hack, self.admin, name='R2', description='second', populate=False)
        first = self.client.post(f'{self.base}import-rounds/')
        self.assertEqual(first.data['created'], 2)
        self.assertEqual([s['title'] for s in first.data['stages']], ['R1', 'R2'])
        self.assertEqual(first.data['stages'][0]['description'], 'first')
        self.assertEqual(self.client.post(f'{self.base}import-rounds/').data['created'], 0)
        RoundService.create_round(self.hack, self.admin, name='R3', populate=False)
        self.assertEqual(self.client.post(f'{self.base}import-rounds/').data['created'], 1)

    def test_admin_only(self):
        player = APIClient()
        player.force_authenticate(make_user())
        self.assertEqual(player.get(self.base).status_code, 403)
        self.assertEqual(player.post(self.base, {'title': 'x'}, format='json').status_code, 403)
        self.assertEqual(APIClient().post(self.base, {'title': 'x'}, format='json').status_code, 401)


class PublicTimelineTests(TestCase):
    def setUp(self):
        self.admin = make_user(role='ADMIN')
        self.hack = make_hackathon(min_team_size=1)
        self.shown = StageService.create(self.hack, self.admin, title='Shown', starts_at=timezone.now(), badge='Online')
        self.hidden = StageService.create(self.hack, self.admin, title='Hidden', is_visible=False)
        self.url = f'/api/hackathons/{self.hack.slug}/stages/'

    def test_public_sees_only_visible_stages_with_safe_fields(self):
        response = APIClient().get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([stage['title'] for stage in response.data], ['Shown'])
        self.assertEqual(
            set(response.data[0]),
            {'id', 'order', 'title', 'description', 'starts_at', 'ends_at', 'badge', 'location', 'tone', 'marker', 'icon', 'link_label', 'link_url'},
        )

    def test_rounds_are_never_exposed_by_themselves(self):
        RoundService.create_round(self.hack, self.admin, name='Secret round', description='internal plan', populate=False)
        titles = [stage['title'] for stage in APIClient().get(self.url).data]
        self.assertNotIn('Secret round', titles)

    def test_hidden_hackathons_and_drafts_do_not_expose_stages(self):
        self.hack.status = 'DRAFT'
        self.hack.save()
        self.assertEqual(APIClient().get(self.url).status_code, 404)

    def test_order_follows_the_admin_order(self):
        third = StageService.create(self.hack, self.admin, title='Third')
        StageService.reorder(self.hack, self.admin, [third.id, self.hidden.id, self.shown.id])
        self.assertEqual([stage['title'] for stage in APIClient().get(self.url).data], ['Third', 'Shown'])
