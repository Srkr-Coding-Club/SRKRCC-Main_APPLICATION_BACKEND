import re
from django.db import models
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.contrib.auth import get_user_model

User = get_user_model()

from apps.accounts.validators import (
    EMAIL_MAX_LENGTH,
    EMAIL_REGEX,
    NAME_MAX_LENGTH,
    NAME_MIN_LENGTH,
    NAME_REGEX,
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    PHONE_NUMBER_LENGTH,
    PHONE_NUMBER_REGEX,
    ROLL_NUMBER_LENGTH,
    ROLL_NUMBER_REGEX,
    normalize_email,
    normalize_name,
    normalize_phone_number,
    normalize_roll_number,
)

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    """
    Custom JWT Serializer embedding user profile data and roles directly into tokens.
    """
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token['email'] = user.email
        token['username'] = user.username
        token['role'] = user.role
        token['first_name'] = user.first_name
        token['last_name'] = user.last_name
        return token

    default_error_messages = {
        # simplejwt's stock wording ("No active account found with the given
        # credentials") reads like a system fault. The sign-in form shows this
        # verbatim, so keep it actionable — and deliberately ambiguous between
        # "wrong email" and "wrong password" so it can't be used to enumerate
        # which emails are registered.
        'no_active_account': 'Incorrect email or password. Please check your details and try again.',
    }

    def validate(self, attrs):
        raw_identifier = str(attrs.get(self.username_field) or '').strip()
        if not raw_identifier:
            raise serializers.ValidationError({self.username_field: "Email is required."})
        if not attrs.get('password'):
            raise serializers.ValidationError({"password": "Password is required."})

        user = User.objects.filter(
            models.Q(email__iexact=raw_identifier) | models.Q(username__iexact=raw_identifier)
        ).first()
        if user:
            from apps.accounts.models import PasswordStatus
            if user.password_status == PasswordStatus.NEEDS_SETUP or not user.has_usable_password():
                raise serializers.ValidationError({
                    "code": "PASSWORD_SETUP_REQUIRED",
                    "detail": "Password setup is required for this account before logging in.",
                })
            # ModelBackend authenticates on an exact USERNAME_FIELD match, so a
            # differently-cased email ("Student@srkr.ac.in") would otherwise fail
            # to sign in to the very account it just created. Hand the backend
            # the stored spelling instead of whatever casing was typed.
            attrs[self.username_field] = user.email

        data = super().validate(attrs)
        data['user'] = {
            'id': self.user.id,
            'email': self.user.email,
            'username': self.user.username,
            'first_name': self.user.first_name,
            'last_name': self.user.last_name,
            'role': self.user.role,
            'club_id': self.user.club_id,
            'membership_status': self.user.membership_status,
            'roll_number': self.user.roll_number,
            'branch': self.user.branch,
            'year': self.user.year,
            'phone_number': self.user.phone_number,
            'github_profile': self.user.github_profile,
            'linkedin_profile': self.user.linkedin_profile,
            'registered_at': self.user.registered_at,
        }
        return data

class UserSerializer(serializers.ModelSerializer):
    referred_by_display = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'username', 'email', 'first_name', 'last_name',
            'role', 'club_id', 'membership_status', 'roll_number', 'branch', 'year', 'phone_number',
            'github_profile', 'linkedin_profile', 'registered_at', 'created_from',
            'referred_by_raw', 'referred_by_display', 'created_at'
        ]
        read_only_fields = ['id', 'role', 'created_at', 'club_id', 'created_from']

    def get_referred_by_display(self, obj):
        if obj.referred_by_user:
            return f"{obj.referred_by_user.first_name} {obj.referred_by_user.last_name}".strip() or obj.referred_by_user.email
        return obj.referred_by_raw or ""

class UserRoleUpdateSerializer(serializers.ModelSerializer):
    """
    Narrow PATCH surface for the admin Users tab — `role`, `membership_status`,
    and `roll_number` are writable here (everything else on User stays
    read-only). Cross-role escalation rules (ADMIN-vs-CLUB_LEAD) apply only to
    `role` and are enforced in the view (UserDetailView.perform_update), since
    they depend on who the requester is. `membership_status` isn't a
    privilege field, so any requester who can already reach this endpoint
    (IsAdminOrClubLead) may set it.

    `roll_number` is deliberately writable here but NOT on the self-service
    PATCH /api/auth/me/ path once a member has already set it (see
    UserProfileDetailSerializer.validate_roll_number) — a member can add
    their own roll number once, but only an admin can add/correct/clear it
    after that.
    """
    class Meta:
        model = User
        fields = ['id', 'role', 'membership_status', 'roll_number']
        read_only_fields = ['id']

    def validate_roll_number(self, value):
        roll = normalize_roll_number(value)
        if not roll:
            return None
        if len(roll) != ROLL_NUMBER_LENGTH:
            raise serializers.ValidationError(
                f"Roll number must be exactly {ROLL_NUMBER_LENGTH} characters (yours has {len(roll)})."
            )
        if not ROLL_NUMBER_REGEX.match(roll):
            raise serializers.ValidationError(
                "Roll number must be alphanumeric only (letters A-Z and digits 0-9), e.g. 21B91A0501."
            )
        existing = User.objects.filter(roll_number__iexact=roll)
        if self.instance is not None:
            existing = existing.exclude(id=self.instance.id)
        if existing.exists():
            raise serializers.ValidationError(
                "This roll number is already registered to another member."
            )
        return roll


class UserProfileDetailSerializer(serializers.ModelSerializer):
    """
    Detailed profile serializer computing real database-driven statistics,
    registered events/forms, streak count, and earned badges.
    """
    streak = serializers.SerializerMethodField()
    points = serializers.SerializerMethodField()
    events_count = serializers.SerializerMethodField()
    projects_count = serializers.SerializerMethodField()
    registered_events = serializers.SerializerMethodField()
    badges = serializers.SerializerMethodField()
    referred_by_display = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'username', 'email', 'first_name', 'last_name', 
            'role', 'club_id', 'membership_status', 'roll_number', 'branch', 'year', 'phone_number',
            'github_profile', 'linkedin_profile', 'registered_at', 'referred_by_raw', 'referred_by_display', 'created_at',
            'streak', 'points', 'events_count', 'projects_count',
            'registered_events', 'badges'
        ]
        # `email` is the account's identity (USERNAME_FIELD, login identifier,
        # and what every notification is addressed to) — it must never change
        # through this self-service endpoint. It used to be missing from this
        # list, meaning a user could silently PATCH their own email here with
        # no validation and no re-verification step.
        read_only_fields = ['id', 'role', 'created_at', 'club_id', 'email']

    def get_referred_by_display(self, obj):
        if obj.referred_by_user:
            return f"{obj.referred_by_user.first_name} {obj.referred_by_user.last_name}".strip() or obj.referred_by_user.email
        return obj.referred_by_raw or ""

    # `first_name`/`last_name`/`roll_number`/`phone_number` are writable
    # through this self-service PATCH /api/auth/me/ endpoint (only
    # id/role/created_at/club_id/email are read-only above), so they must be
    # held to the exact same rules RegisterSerializer enforces at signup —
    # otherwise a user could PATCH their own name to contain digits, or their
    # roll number to a malformed value, bypassing every signup-time check.
    # These reuse the same shared helpers/constants from
    # apps/accounts/validators.py that RegisterSerializer imports above, so
    # the two can never drift apart.
    def validate_first_name(self, value):
        return self._validate_name(value, "First name", required=True)

    def validate_last_name(self, value):
        return self._validate_name(value, "Last name", required=False)

    def _validate_name(self, value, label, required):
        name = normalize_name(value)
        if not name:
            if required:
                raise serializers.ValidationError(f"{label} is required.")
            return ""
        if len(name) < NAME_MIN_LENGTH:
            raise serializers.ValidationError(f"{label} must be at least {NAME_MIN_LENGTH} characters long.")
        if len(name) > NAME_MAX_LENGTH:
            raise serializers.ValidationError(f"{label} must be at most {NAME_MAX_LENGTH} characters long.")
        if not NAME_REGEX.match(name):
            raise serializers.ValidationError(
                f"{label} may only contain letters, spaces, hyphens and apostrophes — no digits or symbols."
            )
        return name

    def validate_roll_number(self, value):
        # Optional field — a user can leave it unset at signup and fill it in
        # later from their profile (the model column is null=True precisely
        # for this). But once they've self-set it, this self-service endpoint
        # locks it — only an admin (via UserRoleUpdateSerializer / the Users
        # tab) can change or clear it after that, so a member can't edit away
        # their own official record once volunteers/attendance have started
        # relying on it.
        roll = normalize_roll_number(value)
        current = self.instance.roll_number if self.instance is not None else None
        if current:
            if roll != current:
                raise serializers.ValidationError(
                    "Your roll number is already set. Contact an admin to change it."
                )
            return current
        if not roll:
            return None
        if len(roll) != ROLL_NUMBER_LENGTH:
            raise serializers.ValidationError(
                f"Roll number must be exactly {ROLL_NUMBER_LENGTH} characters (yours has {len(roll)})."
            )
        if not ROLL_NUMBER_REGEX.match(roll):
            raise serializers.ValidationError(
                "Roll number must be alphanumeric only (letters A-Z and digits 0-9), e.g. 21B91A0501."
            )
        # Exclude the current user's own row — otherwise re-saving an
        # unchanged roll number would be rejected as "already taken by
        # themselves."
        existing = User.objects.filter(roll_number__iexact=roll)
        if self.instance is not None:
            existing = existing.exclude(id=self.instance.id)
        if existing.exists():
            raise serializers.ValidationError(
                "This roll number is already registered. If this is your roll number, sign in instead, "
                "or contact a club representative if you believe this is a mistake."
            )
        return roll

    def validate_phone_number(self, value):
        # blank/null is allowed (the model field is optional) — a user can
        # still clear a previously-set phone number.
        if not value or not value.strip():
            return ""
        phone = normalize_phone_number(value)
        if len(phone) != PHONE_NUMBER_LENGTH or not PHONE_NUMBER_REGEX.match(phone):
            raise serializers.ValidationError(
                f"Phone number must be exactly {PHONE_NUMBER_LENGTH} digits, numbers only."
            )
        return phone

    def get_streak(self, obj):
        try:
            return obj.streak.current_streak
        except Exception:
            return 0

    def get_points(self, obj):
        points = 0
        try:
            # 50 points per correct problem solved
            points += obj.codequest_submissions.filter(is_correct=True).count() * 50
        except Exception:
            pass
        try:
            from apps.forms.models import Response
            # 25 points per form/event registration
            points += Response.objects.filter(user=obj, is_test_submission=False).count() * 25
        except Exception:
            pass
        return max(points, 50)  # Baseline onboarding points

    def get_events_count(self, obj):
        try:
            from apps.forms.models import Response
            return Response.objects.filter(user=obj, is_test_submission=False).count()
        except Exception:
            return 0

    def get_projects_count(self, obj):
        try:
            from apps.hackathons.models import Team
            return Team.objects.filter(models.Q(leader=obj) | models.Q(members=obj)).distinct().count()
        except Exception:
            return 0

    def get_registered_events(self, obj):
        events_list = []
        try:
            from apps.forms.models import Response
            user_responses = Response.objects.filter(user=obj, is_test_submission=False).select_related('form').order_by('-submitted_at')
            for resp in user_responses:
                form = resp.form
                events_list.append({
                    'id': resp.id,
                    'form_id': form.id,
                    'form_slug': form.slug,
                    'title': form.title,
                    'track': form.category or 'General Track',
                    'date': str(form.open_at or resp.submitted_at.date() if resp.submitted_at else 'Active'),
                    'status': 'Seat Confirmed' if form.status == 'PUBLISHED' else 'Registration Received',
                    'badgeBg': 'bg-emerald-50 dark:bg-emerald-950/40 text-emerald-600 dark:text-emerald-400' if form.status == 'PUBLISHED' else 'bg-orange-50 dark:bg-orange-950/40 text-[#FF7A00]',
                    # Drives the "View QR Badge" action on the profile page's
                    # registered-events list (Profile → Registered Events →
                    # select event) — the attendance QR pass moved here from
                    # the form page, so the profile needs to know which
                    # registrations actually have a badge to show.
                    'attendance_enabled': bool(form.attendance_enabled),
                })
        except Exception:
            pass
        return events_list

    def get_badges(self, obj):
        badges = [
            {
                'id': 'verified_member',
                'title': 'SRKRCC Member',
                'description': f"Active {obj.get_role_display() if hasattr(obj, 'get_role_display') else obj.role} of SRKR Coding Club",
                'icon': 'shield',
                'tone': 'emerald'
            }
        ]
        try:
            if hasattr(obj, 'streak') and obj.streak.current_streak >= 5:
                badges.append({
                    'id': 'streak_master',
                    'title': 'Streak Master',
                    'description': f"Maintained {obj.streak.current_streak}+ consecutive days of problem solving",
                    'icon': 'flame',
                    'tone': 'orange'
                })
        except Exception:
            pass

        if obj.role in ['ADMIN', 'CLUB_LEAD']:
            badges.append({
                'id': 'lead_badge',
                'title': 'Executive Lead',
                'description': 'Authorized administrative and club management privileges',
                'icon': 'award',
                'tone': 'purple'
            })
        return badges

class RegisterSerializer(serializers.ModelSerializer):
    """
    Public self-registration.

    Email is the account's identity: it is the USERNAME_FIELD, it is what the
    login form asks for, and it is checked case-insensitively for uniqueness
    here so "Student@srkr.ac.in" cannot shadow "student@srkr.ac.in".

    `username` is NOT accepted from the client. It used to be sent by the signup
    page as `email.split('@')[0]`, which meant two people with the same local
    part at different domains ("a@srkr.ac.in", "a@gmail.com") collided on the
    unique username column and the second signup failed with "A user with that
    username already exists." — an error naming a field the user never filled
    in. It is now derived server-side and de-duplicated.

    Field rules live in apps/accounts/validators.py and are mirrored by the
    frontend in src/lib/validation/auth.ts.
    """
    password = serializers.CharField(
        write_only=True,
        min_length=PASSWORD_MIN_LENGTH,
        max_length=PASSWORD_MAX_LENGTH,
    )
    email = serializers.EmailField(max_length=EMAIL_MAX_LENGTH)
    first_name = serializers.CharField(max_length=NAME_MAX_LENGTH)
    last_name = serializers.CharField(required=False, allow_blank=True, max_length=NAME_MAX_LENGTH)
    roll_number = serializers.CharField(max_length=ROLL_NUMBER_LENGTH * 2, required=False, allow_blank=True, allow_null=True)
    branch = serializers.CharField()
    year = serializers.IntegerField()
    # Derived from email: echoed back in the response, never read from the request.
    username = serializers.CharField(read_only=True)

    # This endpoint is called by two things: the public signup form (which
    # only ever sends AFFILIATE or NON_AFFILIATE — its "are you an affiliate?"
    # checkbox) and the admin's "Create New User" modal (which can also
    # directly create a VOLUNTEER, same as it could before this role split —
    # that capability isn't being removed here, just kept working under the
    # new names). JUDGE/CLUB_LEAD/ADMIN can only be granted by an existing
    # admin via the Users-tab PATCH — role was previously unrestricted here,
    # letting an anonymous POST with {"role": "ADMIN"} create a full admin
    # account. VOLUNTEER must go through the same gate: this endpoint has no
    # permission_classes restriction (AllowAny, since it's also the public
    # signup form), so without a caller check here, anyone could self-grant
    # VOLUNTEER — which is enough to reach the attendance-scan endpoint
    # (apps.attendance.permissions.IsVolunteerOrAbove) — via a direct API call.
    SELF_REGISTERABLE_ROLES = {'AFFILIATE', 'NON_AFFILIATE'}
    ADMIN_GRANTABLE_ROLES = SELF_REGISTERABLE_ROLES | {'VOLUNTEER'}
    role = serializers.CharField(required=False, allow_blank=True)

    # Branches offered on the signup form, kept in sync with the <select> in
    # src/app/signup/page.tsx — free-text branches would poison the admin
    # directory's branch filter and the per-branch analytics.
    ALLOWED_BRANCHES = {'CSE', 'IT', 'AIML', 'AIDS', 'ECE', 'EEE', 'MECH', 'CIVIL'}
    MIN_YEAR = 1
    MAX_YEAR = 4

    # Optional "Affiliate ID" on the signup form — a club representative sometimes
    # hands a prospective member their Club ID before they ever touch the site
    # (e.g. at an offline recruitment drive). If they have one, it's attached to
    # the account they create here; if not, this stays blank and club_id is
    # assigned later the normal way (admin action / CSV import), same as today.
    club_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    class Meta:
        model = User
        fields = [
            'username', 'email', 'password', 'first_name', 'last_name',
            'role', 'roll_number', 'branch', 'year', 'club_id',
        ]

    def validate_email(self, value):
        email = normalize_email(value)
        if not EMAIL_REGEX.match(email):
            raise serializers.ValidationError("Enter a valid email address, for example student@srkr.ac.in.")
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError(
                "An account with this email already exists. Sign in instead, or use "
                "“Forgot / Set Up?” on the sign-in page to recover it."
            )
        return email

    def validate_first_name(self, value):
        return self._validate_name(value, "First name", required=True)

    def validate_last_name(self, value):
        return self._validate_name(value, "Last name", required=False)

    def _validate_name(self, value, label, required):
        name = normalize_name(value)
        if not name:
            if required:
                raise serializers.ValidationError(f"{label} is required.")
            return ""
        if len(name) < NAME_MIN_LENGTH:
            raise serializers.ValidationError(f"{label} must be at least {NAME_MIN_LENGTH} characters long.")
        if len(name) > NAME_MAX_LENGTH:
            raise serializers.ValidationError(f"{label} must be at most {NAME_MAX_LENGTH} characters long.")
        if not NAME_REGEX.match(name):
            raise serializers.ValidationError(
                f"{label} may only contain letters, spaces, hyphens and apostrophes — no digits or symbols."
            )
        return name

    def validate_roll_number(self, value):
        # Optional at signup — a member can add it later from their profile.
        roll = normalize_roll_number(value)
        if not roll:
            return None
        if len(roll) != ROLL_NUMBER_LENGTH:
            raise serializers.ValidationError(
                f"Roll number must be exactly {ROLL_NUMBER_LENGTH} characters (yours has {len(roll)})."
            )
        if not ROLL_NUMBER_REGEX.match(roll):
            raise serializers.ValidationError(
                "Roll number must be alphanumeric only (letters A-Z and digits 0-9), e.g. 21B91A0501."
            )
        if User.objects.filter(roll_number__iexact=roll).exists():
            raise serializers.ValidationError(
                "This roll number is already registered. If this is your roll number, sign in instead, "
                "or contact a club representative if you believe this is a mistake."
            )
        return roll

    def validate_branch(self, value):
        branch = (value or "").strip().upper()
        if branch not in self.ALLOWED_BRANCHES:
            allowed = ", ".join(sorted(self.ALLOWED_BRANCHES))
            raise serializers.ValidationError(f"Select a valid branch. Allowed values: {allowed}.")
        return branch

    def validate_year(self, value):
        if value is None or not (self.MIN_YEAR <= int(value) <= self.MAX_YEAR):
            raise serializers.ValidationError(
                f"Year of study must be between {self.MIN_YEAR} and {self.MAX_YEAR}."
            )
        return int(value)

    def validate_role(self, value):
        requester = getattr(self.context.get('request'), 'user', None)
        is_admin_caller = bool(requester and requester.is_authenticated and (
            requester.is_staff or requester.is_superuser or getattr(requester, 'role', None) in ('ADMIN', 'CLUB_LEAD')
        ))
        allowed = self.ADMIN_GRANTABLE_ROLES if is_admin_caller else self.SELF_REGISTERABLE_ROLES
        return value if value in allowed else 'NON_AFFILIATE'

    def validate_club_id(self, value):
        if not value or not value.strip():
            return None
        from apps.accounts.services.club_id_service import ClubIDService, InvalidClubIdError
        try:
            parsed = ClubIDService.parse_club_id(value)
        except InvalidClubIdError as ex:
            raise serializers.ValidationError(str(ex))
        canonical = parsed['canonical_id']
        if User.objects.filter(club_id__iexact=canonical).exists():
            raise serializers.ValidationError(f"Club ID '{canonical}' is already assigned to another member.")
        return canonical

    def validate(self, attrs):
        # Password strength is checked here rather than in validate_password()
        # because UserAttributeSimilarityValidator needs the rest of the payload:
        # it is what rejects a password built out of the applicant's own email or
        # name, and DRF runs per-field validators before those siblings exist.
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError as DjangoValidationError

        password = attrs.get('password')
        if password:
            email = attrs.get('email', '')
            candidate = User(
                email=email,
                username=email.split('@')[0],
                first_name=attrs.get('first_name', ''),
                last_name=attrs.get('last_name', ''),
            )
            try:
                validate_password(password, user=candidate)
            except DjangoValidationError as ex:
                raise serializers.ValidationError({'password': list(ex.messages)})

        # AFFILIATE always has a club_id (validate_club_id() above has already
        # normalized it to canonical form, or to None if blank/omitted — this
        # runs after both validate_role() and validate_club_id() since DRF
        # calls per-field validators before this object-level one).
        if attrs.get('role') == 'AFFILIATE' and not attrs.get('club_id'):
            raise serializers.ValidationError({
                'club_id': "Affiliate members must provide a valid Club ID. "
                           "If you don't have one yet, sign up as a Non-Affiliate instead.",
            })
        return attrs

    @staticmethod
    def _derive_username(email: str) -> str:
        """
        Builds a unique username from the email local part.

        Collisions are resolved with a numeric suffix rather than surfaced to the
        applicant — username is an internal artifact of AbstractUser here, not
        something the signup form ever asks for.
        """
        base = re.sub(r'[^a-z0-9._-]', '', email.split('@')[0].lower()) or 'member'
        base = base[:140]
        candidate = base
        suffix = 1
        while User.objects.filter(username__iexact=candidate).exists():
            suffix += 1
            tail = str(suffix)
            candidate = f"{base[:140 - len(tail)]}{tail}"
        return candidate

    def create(self, validated_data):
        from django.db import IntegrityError, transaction

        email = validated_data['email']
        roll_number = validated_data.get('roll_number') or None
        club_id = validated_data.get('club_id') or None
        username = self._derive_username(email)
        try:
            # atomic() so an IntegrityError rolls back to this savepoint rather
            # than leaving the outer transaction aborted — without it, the
            # exists() lookups in the except block below would themselves fail
            # with "current transaction is aborted" on Postgres.
            with transaction.atomic():
                user = User.objects.create_user(
                    username=username,
                    email=email,
                    password=validated_data['password'],
                    first_name=validated_data.get('first_name', ''),
                    last_name=validated_data.get('last_name', ''),
                    roll_number=roll_number,
                    branch=validated_data.get('branch', ''),
                    year=validated_data.get('year', None),
                    role=validated_data.get('role') or 'NON_AFFILIATE',
                    club_id=club_id,
                )
        except IntegrityError:
            # Two requests can both pass the pre-save exists() checks above and
            # then race each other to INSERT — the unique DB constraints are the
            # real backstop, this just turns that race into the same friendly,
            # field-anchored error the normal path returns instead of a 500.
            if email and User.objects.filter(email__iexact=email).exists():
                raise serializers.ValidationError({'email': ["An account with this email already exists. Sign in instead."]})
            if roll_number and User.objects.filter(roll_number__iexact=roll_number).exists():
                raise serializers.ValidationError({'roll_number': ["This roll number is already registered."]})
            if club_id and User.objects.filter(club_id__iexact=club_id).exists():
                raise serializers.ValidationError({'club_id': [f"Club ID '{club_id}' is already assigned to another member."]})
            raise serializers.ValidationError("Registration failed due to a conflicting record. Please try again.")
        return user
