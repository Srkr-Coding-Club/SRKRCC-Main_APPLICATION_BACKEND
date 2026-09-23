"""
Server-side field rules for POST /api/auth/register/ and POST /api/auth/login/.

These mirror src/lib/validation/auth.ts on the frontend. The frontend copy is a
convenience so users get instant feedback; this suite is what actually protects
the database, so every rule is asserted against the API, not the serializer.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

User = get_user_model()

VALID_PASSWORD = 'Str0ng!Pass'


class RegistrationValidationTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _payload(self, **overrides):
        payload = {
            'email': 'newmember@srkr.ac.in',
            'password': VALID_PASSWORD,
            'first_name': 'New',
            'last_name': 'Member',
            'roll_number': '22B91A0599',
            'branch': 'CSE',
            'year': 2,
        }
        payload.update(overrides)
        return payload

    def _post(self, **overrides):
        return self.client.post('/api/auth/register/', self._payload(**overrides), format='json')

    # --- Name -------------------------------------------------------------

    def test_name_with_digits_is_rejected(self):
        resp = self._post(first_name='John123')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('first_name', resp.data)

    def test_name_with_symbols_is_rejected(self):
        resp = self._post(first_name='John@Doe')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('first_name', resp.data)

    def test_single_character_name_is_rejected(self):
        resp = self._post(first_name='J')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('first_name', resp.data)

    def test_name_with_hyphen_and_apostrophe_is_accepted(self):
        resp = self._post(first_name="Mary-Jane", last_name="O'Brien")
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_last_name_may_be_omitted(self):
        resp = self._post(last_name='')
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_name_whitespace_is_collapsed(self):
        resp = self._post(first_name='  Ravi   Kumar  ')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(User.objects.get(email='newmember@srkr.ac.in').first_name, 'Ravi Kumar')

    # --- Email ------------------------------------------------------------

    def test_malformed_email_is_rejected(self):
        for bad in ['plainstring', 'no@tld', 'two..dots@srkr.ac.in', '@srkr.ac.in', 'trailing.@srkr.ac.in']:
            with self.subTest(email=bad):
                resp = self._post(email=bad)
                self.assertEqual(resp.status_code, 400, f'{bad} should be rejected')
                self.assertIn('email', resp.data)

    def test_duplicate_email_error_names_the_email_field(self):
        User.objects.create_user(username='taken', email='newmember@srkr.ac.in', password=VALID_PASSWORD)
        resp = self._post()
        self.assertEqual(resp.status_code, 400)
        self.assertIn('email', resp.data)
        self.assertNotIn('username', resp.data)
        self.assertIn('already exists', str(resp.data['email'][0]))

    def test_duplicate_email_is_detected_case_insensitively(self):
        User.objects.create_user(username='taken', email='newmember@srkr.ac.in', password=VALID_PASSWORD)
        resp = self._post(email='NewMember@SRKR.ac.in')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('email', resp.data)

    def test_email_is_stored_lowercased(self):
        resp = self._post(email='MixedCase@SRKR.ac.in')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertTrue(User.objects.filter(email='mixedcase@srkr.ac.in').exists())

    # --- Username derivation ---------------------------------------------

    def test_username_is_derived_and_not_taken_from_the_client(self):
        resp = self._post(username='attacker-supplied')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(User.objects.get(email='newmember@srkr.ac.in').username, 'newmember')

    def test_same_local_part_on_a_different_domain_does_not_collide(self):
        """The bug behind the bogus 'username already exists' signup error."""
        User.objects.create_user(username='newmember', email='newmember@gmail.com', password=VALID_PASSWORD)
        resp = self._post(email='newmember@srkr.ac.in')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(User.objects.get(email='newmember@srkr.ac.in').username, 'newmember2')

    # --- Roll number ------------------------------------------------------

    def test_roll_number_shorter_than_ten_is_rejected(self):
        resp = self._post(roll_number='22B91A05')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('roll_number', resp.data)

    def test_roll_number_longer_than_ten_is_rejected(self):
        resp = self._post(roll_number='22B91A05991')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('roll_number', resp.data)

    def test_roll_number_with_symbols_is_rejected(self):
        resp = self._post(roll_number='22B91A-599')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('roll_number', resp.data)

    def test_roll_number_may_be_omitted(self):
        """Optional field — a member can sign up without one and add it later
        from their profile."""
        payload = self._payload()
        del payload['roll_number']
        resp = self.client.post('/api/auth/register/', payload, format='json')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertIsNone(User.objects.get(email='newmember@srkr.ac.in').roll_number)

    def test_blank_roll_number_is_accepted(self):
        resp = self._post(roll_number='')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertIsNone(User.objects.get(email='newmember@srkr.ac.in').roll_number)

    def test_two_members_can_both_omit_roll_number(self):
        """Regression guard: clearing an empty roll number to NULL (rather
        than storing '') is what lets two different rows both have no roll
        number without violating the unique constraint."""
        payload = self._payload()
        del payload['roll_number']
        resp1 = self.client.post('/api/auth/register/', payload, format='json')
        self.assertEqual(resp1.status_code, 201, resp1.data)

        payload2 = self._payload(email='secondmember@srkr.ac.in')
        del payload2['roll_number']
        resp2 = self.client.post('/api/auth/register/', payload2, format='json')
        self.assertEqual(resp2.status_code, 201, resp2.data)

    def test_roll_number_is_uppercased(self):
        resp = self._post(roll_number='22b91a0599')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(User.objects.get(email='newmember@srkr.ac.in').roll_number, '22B91A0599')

    def test_duplicate_roll_number_is_rejected(self):
        User.objects.create_user(
            username='existing', email='existing@srkr.ac.in', password=VALID_PASSWORD,
            roll_number='21B91A0599',
        )
        resp = self._post(roll_number='21B91A0599')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('roll_number', resp.data)
        self.assertFalse(User.objects.filter(email='newmember@srkr.ac.in').exists())

    def test_duplicate_roll_number_is_detected_case_insensitively(self):
        User.objects.create_user(
            username='existing', email='existing@srkr.ac.in', password=VALID_PASSWORD,
            roll_number='21B91A0599',
        )
        resp = self._post(roll_number='21b91a0599')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('roll_number', resp.data)

    def test_duplicate_roll_number_race_is_still_rejected_cleanly(self):
        """
        Simulates two requests both passing the pre-save exists() check and
        racing to INSERT — the DB's unique constraint is the real backstop, and
        create() must turn that IntegrityError into a normal 400, not a 500.
        """
        from unittest.mock import patch

        from apps.accounts.serializers import RegisterSerializer

        real_derive_username = RegisterSerializer._derive_username

        def derive_username_then_insert_duplicate(email):
            # Runs after RegisterSerializer.create() computes the username but
            # before it opens the atomic() block around create_user() — i.e.
            # before any savepoint exists, so this insert survives even though
            # the real create_user() call below it is about to fail and roll
            # its own savepoint back. That's what makes this a faithful stand-in
            # for a genuinely concurrent request's already-committed row.
            User.objects.create(username='racer', email='racer@srkr.ac.in', roll_number='21B91A0599')
            return real_derive_username(email)

        with patch.object(RegisterSerializer, '_derive_username', staticmethod(derive_username_then_insert_duplicate)):
            resp = self._post(roll_number='21B91A0599')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('roll_number', resp.data)

    # --- Branch and year --------------------------------------------------

    def test_unknown_branch_is_rejected(self):
        resp = self._post(branch='HOGWARTS')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('branch', resp.data)

    def test_year_outside_one_to_four_is_rejected(self):
        for bad_year in [0, 5, -1]:
            with self.subTest(year=bad_year):
                resp = self._post(year=bad_year)
                self.assertEqual(resp.status_code, 400)
                self.assertIn('year', resp.data)

    # --- Phone number -------------------------------------------------------

    def test_phone_number_may_be_omitted(self):
        resp = self._post(email='no-phone@srkr.ac.in', roll_number='22B91A0511')
        self.assertEqual(resp.status_code, 201)

    def test_blank_phone_number_is_accepted(self):
        resp = self._post(email='blank-phone@srkr.ac.in', roll_number='22B91A0512', phone_number='')
        self.assertEqual(resp.status_code, 201)

    def test_phone_number_with_wrong_digit_count_is_rejected(self):
        resp = self._post(phone_number='12345')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('phone_number', resp.data)

    def test_phone_number_with_letters_is_rejected(self):
        resp = self._post(phone_number='9876543abc')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('phone_number', resp.data)

    def test_phone_number_with_separators_is_normalized_and_accepted(self):
        resp = self._post(email='formatted-phone@srkr.ac.in', roll_number='22B91A0513', phone_number='987-654-3210')
        self.assertEqual(resp.status_code, 201)
        user = User.objects.get(email='formatted-phone@srkr.ac.in')
        self.assertEqual(user.phone_number, '9876543210')

    # --- Password ---------------------------------------------------------

    def test_weak_passwords_are_rejected(self):
        cases = {
            'short': 'Ab1!c',
            'no_uppercase': 'str0ng!pass',
            'no_lowercase': 'STR0NG!PASS',
            'no_digit': 'Strong!Pass',
            'no_special': 'Str0ngPass1',
            'has_space': 'Str0ng! Pass',
        }
        for label, pw in cases.items():
            with self.subTest(case=label):
                resp = self._post(password=pw)
                self.assertEqual(resp.status_code, 400, f'{label} should be rejected')
                self.assertIn('password', resp.data)

    def test_password_built_from_the_users_own_email_is_rejected(self):
        resp = self._post(email='ravikumar@srkr.ac.in', password='Ravikumar1!')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('password', resp.data)

    # --- Role -------------------------------------------------------------

    def test_self_registration_cannot_grant_itself_admin(self):
        resp = self._post(role='ADMIN')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(User.objects.get(email='newmember@srkr.ac.in').role, 'NON_AFFILIATE')

    # --- AFFILIATE / NON_AFFILIATE ------------------------------------------

    def test_self_registration_defaults_to_non_affiliate_with_no_role_sent(self):
        resp = self._post()  # _payload() never sets 'role'
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(User.objects.get(email='newmember@srkr.ac.in').role, 'NON_AFFILIATE')

    def test_non_affiliate_signup_does_not_require_a_club_id(self):
        resp = self._post(role='NON_AFFILIATE')
        self.assertEqual(resp.status_code, 201, resp.data)
        user = User.objects.get(email='newmember@srkr.ac.in')
        self.assertEqual(user.role, 'NON_AFFILIATE')
        self.assertIsNone(user.club_id)

    def test_affiliate_signup_without_club_id_is_rejected(self):
        resp = self._post(role='AFFILIATE')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('club_id', resp.data)
        self.assertFalse(User.objects.filter(email='newmember@srkr.ac.in').exists())

    def test_affiliate_signup_with_blank_club_id_is_rejected(self):
        resp = self._post(role='AFFILIATE', club_id='')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('club_id', resp.data)

    def test_affiliate_signup_with_valid_club_id_succeeds(self):
        resp = self._post(role='AFFILIATE', club_id='25SCC410')
        self.assertEqual(resp.status_code, 201, resp.data)
        user = User.objects.get(email='newmember@srkr.ac.in')
        self.assertEqual(user.role, 'AFFILIATE')
        self.assertEqual(user.club_id, '25SCC410')

    def test_affiliate_signup_with_malformed_club_id_is_rejected_by_club_id_check_first(self):
        # validate_club_id() runs before the cross-field AFFILIATE check, so a
        # malformed id is reported as a format problem, not as "missing".
        resp = self._post(role='AFFILIATE', club_id='not-a-club-id')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('club_id', resp.data)
        # "must provide a valid Club ID" is the cross-field validate() message;
        # it must NOT be the one that fired here (checked instead of the naive
        # substring 'valid Club ID', which false-positives against "Invalid
        # Club ID format" — "Invalid" itself contains "valid").
        self.assertNotIn('must provide a valid Club ID', str(resp.data['club_id']))

    def test_anonymous_self_registration_cannot_grant_volunteer(self):
        # This endpoint is AllowAny (it's also the public signup form), so an
        # anonymous POST with role='VOLUNTEER' must be downgraded the same way
        # ADMIN/CLUB_LEAD already are — VOLUNTEER is enough to reach the
        # attendance-scan endpoint (IsVolunteerOrAbove), so letting anyone
        # self-grant it would be a privilege escalation.
        resp = self._post(role='VOLUNTEER')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(User.objects.get(email='newmember@srkr.ac.in').role, 'NON_AFFILIATE')

    def test_admin_caller_can_still_create_volunteer_via_this_endpoint(self):
        # The admin's "Create New User" modal POSTs to this same endpoint, so
        # VOLUNTEER stays reachable when the caller is themselves an
        # authenticated ADMIN/CLUB_LEAD.
        admin = User.objects.create_user(
            username='admin', email='admin@srkr.ac.in', password=VALID_PASSWORD, role='ADMIN', is_staff=True,
        )
        self.client.force_authenticate(admin)
        resp = self._post(role='VOLUNTEER')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(User.objects.get(email='newmember@srkr.ac.in').role, 'VOLUNTEER')


class LoginValidationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='member', email='member@srkr.ac.in', password=VALID_PASSWORD,
        )

    def _login(self, **body):
        return self.client.post('/api/auth/login/', body, format='json')

    def test_login_succeeds_with_the_exact_email(self):
        resp = self._login(email='member@srkr.ac.in', password=VALID_PASSWORD)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertIn('access', resp.data)

    def test_login_is_case_insensitive_on_email(self):
        resp = self._login(email='Member@SRKR.ac.in', password=VALID_PASSWORD)
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_login_ignores_surrounding_whitespace(self):
        resp = self._login(email='  member@srkr.ac.in  ', password=VALID_PASSWORD)
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_missing_email_is_reported_against_the_email_field(self):
        resp = self._login(email='', password=VALID_PASSWORD)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('email', resp.data)

    def test_missing_password_is_reported_against_the_password_field(self):
        resp = self._login(email='member@srkr.ac.in', password='')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('password', resp.data)

    def test_wrong_password_does_not_reveal_whether_the_email_exists(self):
        known = self._login(email='member@srkr.ac.in', password='Wr0ng!Pass')
        unknown = self._login(email='nobody@srkr.ac.in', password='Wr0ng!Pass')
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(str(known.data.get('detail')), str(unknown.data.get('detail')))
        self.assertIn('Incorrect email or password', str(known.data.get('detail')))
