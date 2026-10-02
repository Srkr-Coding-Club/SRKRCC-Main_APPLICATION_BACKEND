from django.utils import timezone
from rest_framework import serializers

from apps.forms.models import ProfileField
from .models import (
    AnnouncementAudience, EntryStatus, Hackathon, HackathonAnnouncement, InviteStatus,
    ProblemStatement, Round, RoundEntry, Submission, Team, TeamInvite, TeamMember, TeamStatus,
)


class HackathonSerializer(serializers.ModelSerializer):
    form_slug = serializers.CharField(source='registration_form.slug', read_only=True)
    form_title = serializers.CharField(source='registration_form.title', read_only=True)
    registration_count = serializers.IntegerField(read_only=True, default=0)
    team_count = serializers.IntegerField(read_only=True, default=0)
    is_hidden = serializers.SerializerMethodField()
    is_registration_open = serializers.BooleanField(read_only=True)

    class Meta:
        model = Hackathon
        fields = [
            'id', 'title', 'slug', 'is_flagship', 'theme', 'description',
            'prize_pool', 'banner_image', 'status', 'start_date', 'end_date',
            'visible_from', 'visible_until', 'is_hidden', 'registration_form',
            'form_slug', 'form_title', 'registration_count', 'team_count',
            'registration_opens_at', 'registration_closes_at', 'min_team_size', 'max_team_size',
            'team_edits_locked', 'required_profile_fields', 'is_registration_open',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['status']

    def get_is_hidden(self, obj) -> bool:
        """See EventSerializer.get_is_hidden — identical window logic."""
        now = timezone.now()
        if obj.visible_from and obj.visible_from > now:
            return True
        if obj.visible_until and obj.visible_until <= now:
            return True
        return False

    def validate_required_profile_fields(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError('Must be a list.')
        allowed = set(ProfileField.values)
        bad = [v for v in value if v not in allowed]
        if bad:
            raise serializers.ValidationError(f"Unknown profile field(s): {', '.join(map(str, bad))}.")
        return list(dict.fromkeys(value))

    def validate(self, attrs):
        lo = attrs.get('min_team_size', getattr(self.instance, 'min_team_size', 1))
        hi = attrs.get('max_team_size', getattr(self.instance, 'max_team_size', 4))
        if lo < 1:
            raise serializers.ValidationError({'min_team_size': 'Must be at least 1.'})
        if hi < lo:
            raise serializers.ValidationError({'max_team_size': 'Must be greater than or equal to the minimum team size.'})
        opens = attrs.get('registration_opens_at', getattr(self.instance, 'registration_opens_at', None))
        closes = attrs.get('registration_closes_at', getattr(self.instance, 'registration_closes_at', None))
        if opens and closes and closes <= opens:
            raise serializers.ValidationError({'registration_closes_at': 'Must be after the registration opening time.'})
        return attrs


# ---------------------------------------------------------------------------
# Problem statements
# ---------------------------------------------------------------------------

class ProblemStatementSerializer(serializers.ModelSerializer):
    team_count = serializers.SerializerMethodField()
    slots_left = serializers.SerializerMethodField()

    class Meta:
        model = ProblemStatement
        fields = [
            'id', 'code', 'title', 'description', 'category', 'tags',
            'max_teams', 'is_active', 'order', 'team_count', 'slots_left',
        ]

    def _count(self, obj):
        annotated = getattr(obj, 'active_team_count', None)
        if annotated is not None:
            return annotated
        return obj.teams.filter(status__in=(TeamStatus.FORMING, TeamStatus.REGISTERED)).count()

    def get_team_count(self, obj):
        return self._count(obj)

    def get_slots_left(self, obj):
        if not obj.max_teams:
            return None
        return max(obj.max_teams - self._count(obj), 0)

    def validate_code(self, value):
        value = (value or '').strip().upper()
        if not value:
            raise serializers.ValidationError('Code is required.')
        hackathon = self.context['hackathon']
        qs = hackathon.problem_statements.filter(code__iexact=value)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError('Another problem statement already uses this code.')
        return value

    def validate_title(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('Title is required.')
        return value

    def validate_tags(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError('Must be a list.')
        return [str(t).strip() for t in value if str(t).strip()]


class ProblemStatementBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProblemStatement
        fields = ['id', 'code', 'title', 'category']


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------

def _user_name(user):
    return f"{user.first_name} {user.last_name}".strip() or user.username or user.email


class TeamMemberSerializer(serializers.ModelSerializer):
    user_id = serializers.IntegerField(source='user.id', read_only=True)
    name = serializers.SerializerMethodField()
    email = serializers.EmailField(source='user.email', read_only=True)
    club_id = serializers.CharField(source='user.club_id', read_only=True)

    class Meta:
        model = TeamMember
        fields = ['user_id', 'name', 'email', 'club_id', 'role', 'joined_at']

    def get_name(self, obj):
        return _user_name(obj.user)


class AdminTeamMemberSerializer(TeamMemberSerializer):
    phone_number = serializers.CharField(source='user.phone_number', read_only=True)
    branch = serializers.CharField(source='user.branch', read_only=True)
    year = serializers.IntegerField(source='user.year', read_only=True)
    roll_number = serializers.CharField(source='user.roll_number', read_only=True)

    class Meta(TeamMemberSerializer.Meta):
        fields = TeamMemberSerializer.Meta.fields + ['phone_number', 'branch', 'year', 'roll_number']


class TeamInviteSerializer(serializers.ModelSerializer):
    team_id = serializers.IntegerField(source='team.id', read_only=True)
    team_name = serializers.CharField(source='team.name', read_only=True)
    hackathon_slug = serializers.CharField(source='hackathon.slug', read_only=True)
    hackathon_title = serializers.CharField(source='hackathon.title', read_only=True)
    invited_user = serializers.SerializerMethodField()
    invited_by_name = serializers.SerializerMethodField()
    problem_statement = ProblemStatementBriefSerializer(source='team.problem_statement', read_only=True)
    member_count = serializers.SerializerMethodField()

    class Meta:
        model = TeamInvite
        fields = [
            'id', 'team_id', 'team_name', 'hackathon_slug', 'hackathon_title',
            'invited_user', 'invited_by_name', 'problem_statement', 'member_count',
            'status', 'created_at', 'responded_at',
        ]

    def get_invited_user(self, obj):
        u = obj.invited_user
        return {'id': u.id, 'name': _user_name(u), 'email': u.email}

    def get_invited_by_name(self, obj):
        return _user_name(obj.invited_by) if obj.invited_by else None

    def get_member_count(self, obj):
        return obj.team.memberships.count()


class TeamSerializer(serializers.ModelSerializer):
    """Participant-facing team view (members' contact details are not exposed)."""
    hackathon_slug = serializers.CharField(source='hackathon.slug', read_only=True)
    problem_statement = ProblemStatementBriefSerializer(read_only=True)
    members = serializers.SerializerMethodField()
    pending_invites = serializers.SerializerMethodField()
    leader_id = serializers.IntegerField(read_only=True)
    member_count = serializers.SerializerMethodField()

    member_serializer_class = TeamMemberSerializer

    class Meta:
        model = Team
        fields = [
            'id', 'name', 'hackathon_slug', 'status', 'problem_statement', 'leader_id',
            'members', 'member_count', 'pending_invites', 'created_at', 'updated_at',
        ]

    def get_members(self, obj):
        memberships = obj.memberships.select_related('user').all()
        return self.member_serializer_class(memberships, many=True).data

    def get_member_count(self, obj):
        return obj.memberships.count()

    def get_pending_invites(self, obj):
        invites = obj.invites.filter(status=InviteStatus.PENDING).select_related('invited_user', 'invited_by', 'hackathon', 'team')
        return TeamInviteSerializer(invites, many=True).data


class RoundEntryBriefSerializer(serializers.ModelSerializer):
    round_id = serializers.IntegerField(source='round.id', read_only=True)
    round_name = serializers.CharField(source='round.name', read_only=True)
    round_order = serializers.IntegerField(source='round.order', read_only=True)
    has_details = serializers.SerializerMethodField()

    class Meta:
        model = RoundEntry
        fields = ['id', 'round_id', 'round_name', 'round_order', 'status', 'feedback', 'admin_notes',
                  'decided_at', 'has_details']

    def get_has_details(self, obj):
        return obj.details_response_id is not None


class AdminTeamSerializer(TeamSerializer):
    member_serializer_class = AdminTeamMemberSerializer
    round_entries = serializers.SerializerMethodField()
    leader_email = serializers.EmailField(source='leader.email', read_only=True)

    class Meta(TeamSerializer.Meta):
        fields = TeamSerializer.Meta.fields + ['leader_email', 'round_entries']

    def get_round_entries(self, obj):
        return RoundEntryBriefSerializer(obj.round_entries.select_related('round').all(), many=True).data


# ---------------------------------------------------------------------------
# Rounds
# ---------------------------------------------------------------------------

class RoundSerializer(serializers.ModelSerializer):
    details_form_slug = serializers.CharField(source='details_form.slug', read_only=True)
    details_form_title = serializers.CharField(source='details_form.title', read_only=True)
    entry_counts = serializers.SerializerMethodField()

    class Meta:
        model = Round
        fields = [
            'id', 'order', 'name', 'description', 'starts_at', 'ends_at', 'status',
            'details_form', 'details_form_slug', 'details_form_title',
            'results_published', 'entry_counts', 'created_at', 'updated_at',
        ]
        read_only_fields = ['results_published']
        extra_kwargs = {'order': {'required': False}}

    def get_entry_counts(self, obj):
        counts = {s: 0 for s in EntryStatus.values}
        for status_value in obj.entries.values_list('status', flat=True):
            counts[status_value] = counts.get(status_value, 0) + 1
        counts['total'] = sum(counts.values())
        counts['details_submitted'] = obj.entries.filter(details_response__isnull=False).count()
        return counts

    def validate_name(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('Round name is required.')
        return value

    def validate_order(self, value):
        hackathon = self.context['hackathon']
        qs = hackathon.rounds.filter(order=value)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError('Another round already uses this position.')
        return value

    def validate(self, attrs):
        starts = attrs.get('starts_at', getattr(self.instance, 'starts_at', None))
        ends = attrs.get('ends_at', getattr(self.instance, 'ends_at', None))
        if starts and ends and ends <= starts:
            raise serializers.ValidationError({'ends_at': 'Must be after the start time.'})
        return attrs


class AdminRoundEntrySerializer(serializers.ModelSerializer):
    team_id = serializers.IntegerField(source='team.id', read_only=True)
    team_name = serializers.CharField(source='team.name', read_only=True)
    team_status = serializers.CharField(source='team.status', read_only=True)
    leader_email = serializers.EmailField(source='team.leader.email', read_only=True)
    member_count = serializers.SerializerMethodField()
    problem_statement = ProblemStatementBriefSerializer(source='team.problem_statement', read_only=True)
    decided_by_name = serializers.SerializerMethodField()
    details_response_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = RoundEntry
        fields = [
            'id', 'team_id', 'team_name', 'team_status', 'leader_email', 'member_count',
            'problem_statement', 'status', 'admin_notes', 'feedback',
            'decided_by_name', 'decided_at', 'details_response_id',
        ]

    def get_member_count(self, obj):
        return obj.team.memberships.count()

    def get_decided_by_name(self, obj):
        return _user_name(obj.decided_by) if obj.decided_by else None


# ---------------------------------------------------------------------------
# Announcements
# ---------------------------------------------------------------------------

class HackathonAnnouncementSerializer(serializers.ModelSerializer):
    """Participant/public view — no audience internals beyond a label."""
    audience_label = serializers.CharField(source='get_audience_display', read_only=True)
    round_name = serializers.CharField(source='round.name', read_only=True)

    class Meta:
        model = HackathonAnnouncement
        fields = ['id', 'title', 'message', 'type', 'audience', 'audience_label', 'round_name', 'publish_at', 'created_at']


class AdminHackathonAnnouncementSerializer(serializers.ModelSerializer):
    audience_label = serializers.CharField(source='get_audience_display', read_only=True)
    round_name = serializers.CharField(source='round.name', read_only=True)
    target_teams = serializers.PrimaryKeyRelatedField(many=True, read_only=True)
    target_team_names = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = HackathonAnnouncement
        fields = [
            'id', 'title', 'message', 'type', 'audience', 'audience_label', 'round', 'round_name',
            'target_teams', 'target_team_names', 'is_active', 'publish_at', 'expires_at',
            'send_email', 'created_by_name', 'created_at', 'updated_at',
        ]

    def get_target_team_names(self, obj):
        return list(obj.target_teams.values_list('name', flat=True))

    def get_created_by_name(self, obj):
        return _user_name(obj.created_by) if obj.created_by else None

    def validate_title(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('Title is required.')
        return value

    def validate_message(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('Message is required.')
        return value

    def validate_round(self, value):
        if value is not None and value.hackathon_id != self.context['hackathon'].id:
            raise serializers.ValidationError('That round belongs to another hackathon.')
        return value

    def validate(self, attrs):
        publish_at = attrs.get('publish_at', getattr(self.instance, 'publish_at', None))
        expires_at = attrs.get('expires_at', getattr(self.instance, 'expires_at', None))
        if publish_at and expires_at and expires_at <= publish_at:
            raise serializers.ValidationError({'expires_at': 'Must be after the publish time.'})
        audience = attrs.get('audience', getattr(self.instance, 'audience', AnnouncementAudience.PUBLIC))
        if audience not in (AnnouncementAudience.ROUND_ALL, AnnouncementAudience.ROUND_SHORTLISTED):
            attrs['round'] = None
        return attrs


class SubmissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Submission
        fields = '__all__'
        # score is judge/admin-only (assigned via Django admin); no grading UI writes it yet,
        # so leaving it writable would let a team self-assign its own hackathon score.
        read_only_fields = ['score']
