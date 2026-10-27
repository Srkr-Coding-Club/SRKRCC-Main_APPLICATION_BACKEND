from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.conf import settings
from django.utils import timezone
from apps.core.models import TimeStampedModel
import uuid

from apps.forms.models import Form, Response as FormResponse, ScanStatus, UploadState
from apps.announcements.models import AnnouncementType


class HackathonStatus(models.TextChoices):
    DRAFT = 'DRAFT', 'Draft'
    LIVE = 'LIVE', 'Live'
    CLOSED = 'CLOSED', 'Closed'


class HackathonMode(models.TextChoices):
    ONLINE = 'ONLINE', 'Online'
    OFFLINE = 'OFFLINE', 'Offline'
    HYBRID = 'HYBRID', 'Hybrid'


class Hackathon(TimeStampedModel):
    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True)
    is_flagship = models.BooleanField(default=False, help_text="Flag as IconCoders flagship edition")
    theme = models.CharField(max_length=255)
    description = models.TextField()
    prize_pool = models.CharField(max_length=100, default='₹50,000')
    banner_image = models.URLField(blank=True, null=True)
    banner_crop = models.JSONField(
        default=dict, blank=True,
        help_text='How the banner is framed: {x, y} focal point in percent (0-100) and zoom (1-3). Empty = centred, no zoom.',
    )

    # --- Public page presentation (all optional, all editable by admins) ----------
    mode = models.CharField(max_length=10, choices=HackathonMode.choices, default=HackathonMode.ONLINE)
    venue = models.CharField(max_length=200, blank=True, help_text='Where it happens, for offline/hybrid events.')
    organizer_name = models.CharField(max_length=150, blank=True, help_text='Shown under the title. Blank = the club.')
    logo_image = models.URLField(blank=True, null=True)
    tags = models.JSONField(default=list, blank=True, help_text='Short category labels shown as chips, e.g. ["AI", "Web"].')
    prizes = models.JSONField(
        default=list, blank=True,
        help_text='List of {title, amount, description} shown in the Prizes section. Blank = only the prize pool.',
    )
    status = models.CharField(max_length=20, choices=HackathonStatus.choices, default=HackathonStatus.LIVE)

    start_date = models.DateTimeField()
    end_date = models.DateTimeField()
    visible_from = models.DateTimeField(blank=True, null=True)
    visible_until = models.DateTimeField(blank=True, null=True)
    hidden_at = models.DateTimeField(
        blank=True, null=True,
        help_text="Set when an admin manually hides the hackathon from the public site. Independent of the visible_from/visible_until window.",
    )

    # Legacy: a generic Form used as the registration link before team
    # formation existed. Not used by the team-registration flow.
    registration_form = models.ForeignKey(Form, on_delete=models.SET_NULL, null=True, blank=True)

    # --- Team registration settings ------------------------------------------
    registration_opens_at = models.DateTimeField(blank=True, null=True)
    registration_closes_at = models.DateTimeField(blank=True, null=True)
    min_team_size = models.PositiveIntegerField(default=1)
    max_team_size = models.PositiveIntegerField(default=4)
    team_edits_locked = models.BooleanField(
        default=False,
        help_text="Freeze team name / problem statement / membership changes by participants (admins can still edit).",
    )
    allow_open_innovation = models.BooleanField(
        default=True,
        help_text="Let teams bring their own problem (title, description, domain) instead of picking an admin-defined statement.",
    )
    problem_domains = models.JSONField(
        default=list, blank=True,
        help_text='Domains problem statements are filed under (e.g. ["AI", "EdTech"]). Admin-configured; statements pick from this list.',
    )
    faq = models.JSONField(
        default=list, blank=True,
        help_text='Organizer-written FAQ: a list of {question, answer}. Empty = the generic default FAQ is shown.',
    )
    required_profile_fields = models.JSONField(
        default=list, blank=True,
        help_text="apps.forms.models.ProfileField keys every member must have filled in on their profile, e.g. ['phone_number', 'branch'].",
    )
    # --- Event operations: every one of these is admin-controlled ---------------
    features = models.JSONField(
        default=dict, blank=True,
        help_text='Switches for optional modules: {submissions, judging, check_in, certificates, team_finder, showcase}. Missing = off.',
    )
    max_teams = models.PositiveIntegerField(blank=True, null=True, help_text='Teams allowed to register. Blank = unlimited.')
    max_participants = models.PositiveIntegerField(blank=True, null=True, help_text='Total participants allowed. Blank = unlimited.')
    waitlist_enabled = models.BooleanField(default=False, help_text='When full, new teams queue instead of being refused.')
    eligibility = models.JSONField(
        default=dict, blank=True,
        help_text='Who may take part: {colleges: [], branches: [], years: []}. An empty list means no restriction.',
    )
    rules = models.TextField(blank=True, help_text='Rules / code of conduct (Markdown).')
    require_rules_acceptance = models.BooleanField(default=False)
    registration_questions = models.JSONField(
        default=list, blank=True,
        help_text='Extra questions every participant answers: [{key, label, type, required, options}].',
    )
    problem_statements_reveal_at = models.DateTimeField(blank=True, null=True, help_text='Non-admins see problem statements only after this time.')
    submission_opens_at = models.DateTimeField(blank=True, null=True)
    submission_deadline = models.DateTimeField(blank=True, null=True)
    idea_opens_at = models.DateTimeField(blank=True, null=True, help_text='Phase 1 (plan): when teams may start submitting ideas.')
    idea_deadline = models.DateTimeField(blank=True, null=True, help_text='Phase 1 (plan): ideas cannot be submitted or edited after this.')
    check_in_opens_at = models.DateTimeField(blank=True, null=True, help_text='Day-of check-in: QR codes work from this time (empty = always).')
    check_in_closes_at = models.DateTimeField(blank=True, null=True, help_text='Day-of check-in: scanning stops after this time (empty = never).')
    idea_review_required = models.BooleanField(default=True, help_text='Organizers must approve an idea before it counts as accepted.')
    idea_fee_amount = models.PositiveIntegerField(default=0, help_text='Rupees a team pays with its idea (0 = no payment). The payment screenshot is then required.')
    idea_payment_qr = models.ForeignKey('IdeaFile', on_delete=models.SET_NULL, null=True, blank=True, related_name='+', help_text='QR code teams scan to pay (image uploaded by the organizers).')
    idea_payment_note = models.TextField(blank=True, help_text='How to pay (UPI id, QR instructions...), shown to teams with the idea form.')
    idea_deck_required = models.BooleanField(default=False, help_text='Teams must upload a .ppt / .pptx with their idea.')
    idea_linkedin_required = models.BooleanField(default=False, help_text='Teams must share the link of their LinkedIn post about the hackathon.')
    idea_edits_enabled = models.BooleanField(default=True, help_text='Teams may edit an idea they already submitted. Off = locked after the first submission (changes the organizers ask for are still allowed).')
    idea_edits_until = models.DateTimeField(blank=True, null=True, help_text='Edits close at this time instead of at the idea deadline (it can be later than the deadline).')
    project_requires_approved_idea = models.BooleanField(
        default=False, help_text='Phase 2 (implement): only teams whose idea is accepted may submit a project.',
    )
    submission_reminder_sent_at = models.DateTimeField(blank=True, null=True, help_text='Set by the send_hackathon_reminders command so each deadline is reminded once.')
    results_published_at = models.DateTimeField(blank=True, null=True, help_text='Set when winners / the leaderboard are made public.')
    show_leaderboard = models.BooleanField(default=False, help_text="Show every team's score on the public results, not only winners.")
    certificate_config = models.JSONField(
        default=dict, blank=True,
        help_text='{title, body, signatory_name, signatory_title, eligibility, released}. See certificates.py.',
    )
    mentors = models.JSONField(default=list, blank=True, help_text='[{name, role, bio, photo_url, link}] shown on the public page.')
    judges = models.JSONField(default=list, blank=True, help_text='[{name, role, bio, photo_url, link}] shown on the public page.')
    sponsors = models.JSONField(default=list, blank=True, help_text='[{name, logo_url, url, tier}] shown on the public page.')
    contacts = models.JSONField(default=list, blank=True, help_text='[{name, role, email, phone}] organizer contacts.')
    community_links = models.JSONField(default=list, blank=True, help_text='[{label, url}] e.g. Discord / WhatsApp.')
    venue_map_url = models.URLField(blank=True, null=True)
    team_email_config = models.JSONField(
        default=dict, blank=True,
        help_text="Per-email overrides for team lifecycle emails: {event: {enabled, subject, body}}. See apps/hackathons/team_emails.py.",
    )

    def __str__(self):
        return self.title

    def feature(self, name: str) -> bool:
        return bool((self.features or {}).get(name, False))

    @property
    def is_registration_open(self) -> bool:
        if self.status != HackathonStatus.LIVE:
            return False
        now = timezone.now()
        if self.registration_opens_at and now < self.registration_opens_at:
            return False
        if self.registration_closes_at and now >= self.registration_closes_at:
            return False
        return True


class ProblemStatement(TimeStampedModel):
    hackathon = models.ForeignKey(Hackathon, on_delete=models.CASCADE, related_name='problem_statements')
    # Generated by ProblemStatementService (PS-001, PS-002, ...) - never typed by admins.
    code = models.CharField(max_length=30, blank=True)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    domain = models.CharField(max_length=100, blank=True)
    tags = models.JSONField(default=list, blank=True)
    max_teams = models.PositiveIntegerField(blank=True, null=True, help_text="Blank = unlimited.")
    is_active = models.BooleanField(default=True)
    order = models.IntegerField(default=0)

    class Meta:
        ordering = ['order', 'id']
        constraints = [
            models.UniqueConstraint(fields=['hackathon', 'code'], name='uniq_problem_statement_code_per_hackathon'),
        ]

    def __str__(self):
        return f"{self.code}: {self.title}"


class TeamStatus(models.TextChoices):
    FORMING = 'FORMING', 'Forming'
    REGISTERED = 'REGISTERED', 'Registered'
    DISQUALIFIED = 'DISQUALIFIED', 'Disqualified'
    WITHDRAWN = 'WITHDRAWN', 'Withdrawn'


class Team(TimeStampedModel):
    hackathon = models.ForeignKey(Hackathon, on_delete=models.CASCADE, related_name='teams')
    name = models.CharField(max_length=150)
    # SET_NULL, not CASCADE: deleting one user must not wipe the whole team
    # (other members + the team's Submission) along with them.
    leader = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='led_teams')
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL, through='TeamMember', related_name='hackathon_teams', blank=True,
    )
    # SET_NULL, not PROTECT: PROTECT made deleting a whole hackathon fail (its
    # problem statements and teams cascade together). Deleting a statement
    # teams have picked is blocked explicitly in ProblemStatementDetailView.
    problem_statement = models.ForeignKey(
        ProblemStatement, on_delete=models.SET_NULL, null=True, blank=True, related_name='teams',
    )
    # Open innovation: instead of an admin-defined statement the team submits
    # its own problem. Mutually exclusive with `problem_statement`.
    is_open_innovation = models.BooleanField(default=False)
    custom_problem_title = models.CharField(max_length=255, blank=True)
    custom_problem_description = models.TextField(blank=True)
    custom_problem_domain = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=20, choices=TeamStatus.choices, default=TeamStatus.FORMING)
    is_waitlisted = models.BooleanField(default=False, help_text='Queued because the hackathon is at capacity.')
    status_reason = models.CharField(max_length=300, blank=True, help_text='Shown to the team, e.g. why it was disqualified.')
    looking_for_members = models.BooleanField(default=False)
    looking_for = models.CharField(max_length=200, blank=True, help_text='Skills / roles the team wants, shown in the team finder.')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='Who created the team themselves (empty when an organizer created it). Counts toward the per-person team limit.',
    )
    withdrawn_at = models.DateTimeField(blank=True, null=True, help_text='When the team was withdrawn by its own leader.')
    withdrawn_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='The leader who withdrew the team (drives the cooldown before they may create another).',
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(Lower('name'), 'hackathon', name='uniq_team_name_per_hackathon_ci'),
        ]

    @property
    def open_innovation_code(self) -> str:
        return f"OI-{self.pk:03d}"

    def __str__(self):
        return f"Team '{self.name}' - {self.hackathon.title}"


class TeamRole(models.TextChoices):
    LEADER = 'LEADER', 'Leader'
    MEMBER = 'MEMBER', 'Member'


class TeamMember(models.Model):
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='memberships')
    # Denormalized from team.hackathon so the DB can enforce one team per
    # user per hackathon with a plain unique constraint.
    hackathon = models.ForeignKey(Hackathon, on_delete=models.CASCADE, related_name='memberships')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='hackathon_memberships')
    role = models.CharField(max_length=10, choices=TeamRole.choices, default=TeamRole.MEMBER)
    joined_at = models.DateTimeField(auto_now_add=True)
    rules_accepted_at = models.DateTimeField(blank=True, null=True)
    answers = models.JSONField(default=dict, blank=True, help_text='Answers to Hackathon.registration_questions, keyed by question key.')

    class Meta:
        ordering = ['joined_at', 'id']
        constraints = [
            models.UniqueConstraint(fields=['hackathon', 'user'], name='uniq_one_team_per_user_per_hackathon'),
        ]

    def save(self, *args, **kwargs):
        if not self.hackathon_id and self.team_id:
            self.hackathon_id = self.team.hackathon_id
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user} in {self.team.name} ({self.role})"


class WithdrawalStatus(models.TextChoices):
    PENDING = 'PENDING', 'Waiting for the organizers'
    APPROVED = 'APPROVED', 'Approved'
    DENIED = 'DENIED', 'Denied'
    CANCELLED = 'CANCELLED', 'Cancelled'


class WithdrawalRequest(TimeStampedModel):
    """A leader asking to withdraw a team that already holds a place (registered, or in a round). Organizers decide."""
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='withdrawal_requests')
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    reason = models.CharField(max_length=300)
    status = models.CharField(max_length=10, choices=WithdrawalStatus.choices, default=WithdrawalStatus.PENDING)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    decided_at = models.DateTimeField(blank=True, null=True)
    decision_note = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(fields=['team'], condition=Q(status='PENDING'), name='uniq_pending_withdrawal_per_team'),
        ]


class TeamAllowance(TimeStampedModel):
    """Organizers let one person create more teams than the usual limit (see withdrawals.MAX_SELF_CREATED_TEAMS)."""
    hackathon = models.ForeignKey(Hackathon, on_delete=models.CASCADE, related_name='team_allowances')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    extra = models.PositiveSmallIntegerField(default=1, help_text='Teams this person may create beyond the usual limit.')
    reason = models.CharField(max_length=200, blank=True)
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['hackathon', 'user'], name='uniq_team_allowance_per_person'),
        ]


class InviteStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending'
    ACCEPTED = 'ACCEPTED', 'Accepted'
    DECLINED = 'DECLINED', 'Declined'
    CANCELLED = 'CANCELLED', 'Cancelled'


class TeamInvite(models.Model):
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='invites')
    hackathon = models.ForeignKey(Hackathon, on_delete=models.CASCADE, related_name='team_invites')
    invited_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='hackathon_invites')
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    status = models.CharField(max_length=10, choices=InviteStatus.choices, default=InviteStatus.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['team', 'invited_user'], condition=Q(status='PENDING'),
                name='uniq_pending_invite_per_team_user',
            ),
        ]

    def __str__(self):
        return f"Invite {self.invited_user} → {self.team.name} ({self.status})"


class JoinRequestStatus(models.TextChoices):
    PENDING = 'PENDING', 'Waiting for the leader'
    ACCEPTED = 'ACCEPTED', 'Accepted'
    DECLINED = 'DECLINED', 'Declined'
    CANCELLED = 'CANCELLED', 'Cancelled'


class TeamJoinRequest(TimeStampedModel):
    """A solo participant asking a recruiting team to take them in (the other direction from TeamInvite)."""
    hackathon = models.ForeignKey(Hackathon, on_delete=models.CASCADE, related_name='join_requests')
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='join_requests')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='hackathon_join_requests')
    message = models.CharField(max_length=300, blank=True)
    registration_data = models.JSONField(default=dict, blank=True, help_text='The rules acceptance and answers to the registration questions, given with the request.')
    status = models.CharField(max_length=10, choices=JoinRequestStatus.choices, default=JoinRequestStatus.PENDING)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    decided_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(fields=['team', 'user'], condition=Q(status='PENDING'), name='uniq_pending_join_request_per_team_user'),
        ]
        indexes = [models.Index(fields=['hackathon', 'user', 'status']), models.Index(fields=['team', 'status'])]

    def __str__(self):
        return f"{self.user} -> {self.team.name} ({self.status})"


class EmailInviteStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending'
    CLAIMED = 'CLAIMED', 'Claimed'
    CANCELLED = 'CANCELLED', 'Cancelled'


class TeamEmailInvite(models.Model):
    """An invitation to an email address with no account yet; it becomes a TeamInvite when they sign up."""
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='email_invites')
    hackathon = models.ForeignKey(Hackathon, on_delete=models.CASCADE, related_name='team_email_invites')
    email = models.EmailField()
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    status = models.CharField(max_length=10, choices=EmailInviteStatus.choices, default=EmailInviteStatus.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(
                Lower('email'), 'team', condition=Q(status='PENDING'), name='uniq_pending_email_invite_per_team',
            ),
        ]

    def __str__(self):
        return f"Email invite {self.email} -> {self.team.name} ({self.status})"


class RoundStatus(models.TextChoices):
    UPCOMING = 'UPCOMING', 'Upcoming'
    ACTIVE = 'ACTIVE', 'Active'
    COMPLETED = 'COMPLETED', 'Completed'


class RoundKind(models.TextChoices):
    GENERAL = 'GENERAL', 'General'
    SCREENING = 'SCREENING', 'Screening'
    IDEA_REVIEW = 'IDEA_REVIEW', 'Idea review'
    PROJECT_REVIEW = 'PROJECT_REVIEW', 'Project review'
    PITCH = 'PITCH', 'Pitch / demo'
    FINAL = 'FINAL', 'Final'


class Round(TimeStampedModel):
    hackathon = models.ForeignKey(Hackathon, on_delete=models.CASCADE, related_name='rounds')
    order = models.PositiveIntegerField()
    name = models.CharField(max_length=150)
    kind = models.CharField(max_length=16, choices=RoundKind.choices, default=RoundKind.GENERAL)
    shortlist_limit = models.PositiveIntegerField(
        blank=True, null=True, help_text='How many teams should go through. Shortlisting more asks for confirmation. Blank = no target.',
    )
    description = models.TextField(blank=True)
    starts_at = models.DateTimeField(blank=True, null=True)
    ends_at = models.DateTimeField(blank=True, null=True)
    status = models.CharField(max_length=10, choices=RoundStatus.choices, default=RoundStatus.UPCOMING)
    details_form = models.ForeignKey(
        Form, on_delete=models.SET_NULL, null=True, blank=True, related_name='hackathon_rounds',
        help_text="Form shortlisted teams' leaders fill in to (re-)submit details for this round.",
    )
    results_published = models.BooleanField(default=False)

    class Meta:
        ordering = ['order', 'id']
        constraints = [
            models.UniqueConstraint(fields=['hackathon', 'order'], name='uniq_round_order_per_hackathon'),
        ]

    def __str__(self):
        return f"{self.hackathon.title}, Round {self.order}: {self.name}"


class StageTone(models.TextChoices):
    ORANGE = 'ORANGE', 'Orange'
    BLUE = 'BLUE', 'Blue'
    GREEN = 'GREEN', 'Green'
    PURPLE = 'PURPLE', 'Purple'
    RED = 'RED', 'Red'
    SLATE = 'SLATE', 'Grey'


class StageMarker(models.TextChoices):
    DATE = 'DATE', 'Date badge'
    ICON = 'ICON', 'Icon'


class StageIcon(models.TextChoices):
    FLAG = 'FLAG', 'Flag'
    CODE = 'CODE', 'Code'
    MIC = 'MIC', 'Presentation'
    TROPHY = 'TROPHY', 'Trophy'
    CALENDAR = 'CALENDAR', 'Calendar'
    USERS = 'USERS', 'Team'
    LIGHTBULB = 'LIGHTBULB', 'Idea'
    ROCKET = 'ROCKET', 'Launch'
    CLIPBOARD = 'CLIPBOARD', 'Submission'
    AWARD = 'AWARD', 'Award'


class HackathonStage(TimeStampedModel):
    """One entry of the public "Stages and Timelines" section. Independent of rounds, so it can
    describe things rounds never model (registration, idea submission, results, ceremonies)."""
    hackathon = models.ForeignKey(Hackathon, on_delete=models.CASCADE, related_name='stages')
    title = models.CharField(max_length=150)
    description = models.TextField(blank=True, help_text='Markdown.')
    starts_at = models.DateTimeField(blank=True, null=True)
    ends_at = models.DateTimeField(blank=True, null=True)
    badge = models.CharField(max_length=40, blank=True, help_text='Small label on the card, e.g. "Online" or "Mandatory".')
    location = models.CharField(max_length=150, blank=True, help_text='Where or how, e.g. "Auditorium" or "Google Meet".')
    tone = models.CharField(max_length=10, choices=StageTone.choices, default=StageTone.ORANGE)
    marker = models.CharField(max_length=5, choices=StageMarker.choices, default=StageMarker.DATE)
    icon = models.CharField(max_length=12, choices=StageIcon.choices, default=StageIcon.FLAG)
    link_label = models.CharField(max_length=40, blank=True)
    link_url = models.URLField(blank=True)
    is_visible = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)
    source_round = models.ForeignKey(Round, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return f"{self.hackathon.title}: {self.title}"


class EntryStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending'
    SHORTLISTED = 'SHORTLISTED', 'Shortlisted'
    REJECTED = 'REJECTED', 'Not Shortlisted'


class RoundEntry(TimeStampedModel):
    round = models.ForeignKey(Round, on_delete=models.CASCADE, related_name='entries')
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='round_entries')
    status = models.CharField(max_length=12, choices=EntryStatus.choices, default=EntryStatus.PENDING)
    admin_notes = models.TextField(blank=True, help_text="Internal only, never shown to the team.")
    feedback = models.TextField(blank=True, help_text="Shown to the team once results are published.")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    decided_at = models.DateTimeField(blank=True, null=True)
    details_response = models.ForeignKey(
        FormResponse, on_delete=models.SET_NULL, null=True, blank=True, related_name='hackathon_round_entries',
    )

    class Meta:
        ordering = ['round__order', 'team__name']
        constraints = [
            models.UniqueConstraint(fields=['round', 'team'], name='uniq_round_entry_per_team'),
        ]

    def __str__(self):
        return f"{self.team.name} @ {self.round.name}: {self.status}"


class AnnouncementAudience(models.TextChoices):
    PUBLIC = 'PUBLIC', 'Everyone (public)'
    PARTICIPANTS = 'PARTICIPANTS', 'All registered participants'
    ROUND_ALL = 'ROUND_ALL', 'All teams in a round'
    ROUND_SHORTLISTED = 'ROUND_SHORTLISTED', 'Shortlisted teams in a round'
    ROUND_REJECTED = 'ROUND_REJECTED', 'Teams not shortlisted in a round'
    TEAMS = 'TEAMS', 'Specific teams'


class HackathonAnnouncement(TimeStampedModel):
    hackathon = models.ForeignKey(Hackathon, on_delete=models.CASCADE, related_name='announcements')
    title = models.CharField(max_length=200)
    message = models.TextField()
    type = models.CharField(max_length=10, choices=AnnouncementType.choices, default=AnnouncementType.INFO)
    audience = models.CharField(max_length=20, choices=AnnouncementAudience.choices, default=AnnouncementAudience.PUBLIC)
    # SET_NULL is a safety net; RoundService.delete_round converts round-scoped
    # announcements to team-scoped ones first so they keep their audience.
    round = models.ForeignKey(Round, on_delete=models.SET_NULL, null=True, blank=True, related_name='announcements')
    target_teams = models.ManyToManyField(Team, blank=True, related_name='targeted_announcements')
    is_active = models.BooleanField(default=True)
    publish_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(blank=True, null=True)
    send_email = models.BooleanField(default=False)
    emailed_at = models.DateTimeField(
        blank=True, null=True, help_text='When the audience email was dispatched; empty = not sent yet.',
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['-publish_at', '-id']

    def __str__(self):
        return f"{self.title} ({self.audience})"


class Submission(TimeStampedModel):
    team = models.OneToOneField(Team, on_delete=models.CASCADE, related_name='submission')
    project_title = models.CharField(max_length=200)
    tagline = models.CharField(max_length=200, blank=True)
    description = models.TextField()
    repo_url = models.URLField()
    demo_url = models.URLField(blank=True, null=True)
    video_url = models.URLField(blank=True, null=True)
    slides_url = models.URLField(blank=True, null=True)
    tech_stack = models.JSONField(default=list, blank=True)
    is_public = models.BooleanField(default=True, help_text='May appear in the public showcase once results are out.')
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    score = models.FloatField(default=0.0, help_text='Average judge score out of 100; maintained by JudgingService.')
    review_count = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"Submission for {self.team.name}: {self.project_title}"


class StaffRole(models.TextChoices):
    ORGANIZER = 'ORGANIZER', 'Organizer'
    JUDGE = 'JUDGE', 'Judge'
    VOLUNTEER = 'VOLUNTEER', 'Volunteer (check-in)'


class HackathonStaff(TimeStampedModel):
    """People with a role limited to one hackathon, whatever their global role."""
    hackathon = models.ForeignKey(Hackathon, on_delete=models.CASCADE, related_name='staff')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='hackathon_staff_roles')
    role = models.CharField(max_length=12, choices=StaffRole.choices)
    permissions = models.JSONField(
        default=list, blank=True,
        help_text='What this person may do in this hackathon (keys from access.PERMISSIONS). Filled from the role preset and editable.',
    )
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['role', 'id']
        constraints = [
            models.UniqueConstraint(fields=['hackathon', 'user', 'role'], name='uniq_staff_role_per_hackathon'),
        ]

    def __str__(self):
        return f"{self.user} - {self.role} @ {self.hackathon.title}"


class CriterionTarget(models.TextChoices):
    PROJECT = 'PROJECT', 'Project'
    IDEA = 'IDEA', 'Idea'


class JudgingCriterion(models.Model):
    hackathon = models.ForeignKey(Hackathon, on_delete=models.CASCADE, related_name='criteria')
    target = models.CharField(max_length=8, choices=CriterionTarget.choices, default=CriterionTarget.PROJECT)
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=300, blank=True)
    max_score = models.PositiveIntegerField(default=10)
    weight = models.PositiveIntegerField(default=1, help_text='Relative weight in the final score.')
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return f"{self.name} (/{self.max_score})"


class SubmissionReview(TimeStampedModel):
    """One judge's scores for one submission: {criterion_id: score}."""
    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name='reviews')
    judge = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='hackathon_reviews')
    scores = models.JSONField(default=dict)
    comment = models.TextField(blank=True)
    total = models.FloatField(default=0.0, help_text="This judge's weighted total out of 100.")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['submission', 'judge'], name='uniq_review_per_judge_per_submission'),
        ]


class HackathonWinner(TimeStampedModel):
    hackathon = models.ForeignKey(Hackathon, on_delete=models.CASCADE, related_name='winners')
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='awards')
    title = models.CharField(max_length=120, help_text='e.g. Winner, Runner-up, Best UI')
    rank = models.PositiveIntegerField(default=1)
    prize = models.CharField(max_length=100, blank=True)
    note = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['rank', 'id']
        constraints = [
            models.UniqueConstraint(fields=['hackathon', 'team', 'title'], name='uniq_award_per_team'),
        ]


class CheckIn(models.Model):
    hackathon = models.ForeignKey(Hackathon, on_delete=models.CASCADE, related_name='check_ins')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='hackathon_check_ins')
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='check_ins')
    checked_in_at = models.DateTimeField(default=timezone.now)
    checked_in_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    override_reason = models.CharField(max_length=200, blank=True, help_text='Why an organizer let in someone whose team was not registered (empty = a normal check-in).')

    class Meta:
        ordering = ['-checked_in_at']
        constraints = [
            models.UniqueConstraint(fields=['hackathon', 'user'], name='uniq_check_in_per_user'),
        ]


class TeamSeeker(TimeStampedModel):
    """A participant without a team who wants to be found (team finder)."""
    hackathon = models.ForeignKey(Hackathon, on_delete=models.CASCADE, related_name='seekers')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='hackathon_seeker_profiles')
    skills = models.CharField(max_length=200, blank=True)
    note = models.CharField(max_length=300, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['-updated_at']
        constraints = [
            models.UniqueConstraint(fields=['hackathon', 'user'], name='uniq_seeker_per_hackathon'),
        ]


class IdeaStatus(models.TextChoices):
    SUBMITTED = 'SUBMITTED', 'Submitted'
    APPROVED = 'APPROVED', 'Approved'
    CHANGES_REQUESTED = 'CHANGES_REQUESTED', 'Changes requested'
    REJECTED = 'REJECTED', 'Rejected'


class IdeaFileKind(models.TextChoices):
    DECK = 'DECK', 'Slides (.ppt / .pptx)'
    PAYMENT = 'PAYMENT', 'Payment screenshot'
    QR = 'QR', 'Payment QR code (set by the organizers)'


class IdeaFile(TimeStampedModel):
    """A file a team uploaded straight to storage for its idea. The payment screenshot is visible to organizers only."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    hackathon = models.ForeignKey(Hackathon, on_delete=models.CASCADE, related_name='idea_files')
    team = models.ForeignKey('Team', on_delete=models.CASCADE, null=True, blank=True, related_name='idea_files', help_text='Empty for the hackathon-wide payment QR code.')
    kind = models.CharField(max_length=8, choices=IdeaFileKind.choices)
    uploader = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    key = models.CharField(max_length=255, unique=True)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=255, blank=True)
    declared_size = models.BigIntegerField(default=0)
    size = models.BigIntegerField(default=0)
    state = models.CharField(max_length=10, choices=UploadState.choices, default=UploadState.PENDING)
    scan_status = models.CharField(max_length=12, choices=ScanStatus.choices, default=ScanStatus.NOT_SCANNED)
    expires_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=['team', 'kind', 'state']), models.Index(fields=['state', 'expires_at'])]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.original_name} ({self.state})"


class IdeaSubmission(TimeStampedModel):
    """Phase 1 (plan): a team's idea, reviewed by organizers before (optionally) unlocking the project phase."""
    team = models.OneToOneField(Team, on_delete=models.CASCADE, related_name='idea')
    title = models.CharField(max_length=200)
    problem = models.TextField(help_text='The problem being solved and for whom.')
    solution = models.TextField(help_text='The approach / what the team will build.')
    plan = models.TextField(blank=True, help_text='Milestones, who does what, how the time will be used.')
    tech_stack = models.JSONField(default=list, blank=True)
    deck_url = models.URLField(blank=True, null=True)
    linkedin_url = models.URLField(blank=True, null=True, help_text='The team\'s LinkedIn post about the hackathon.')
    transaction_id = models.CharField(max_length=60, blank=True, help_text='Payment reference (organizers only).')
    deck_file = models.ForeignKey(IdeaFile, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    payment_proof = models.ForeignKey(IdeaFile, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    status = models.CharField(max_length=20, choices=IdeaStatus.choices, default=IdeaStatus.SUBMITTED)
    score = models.FloatField(default=0.0, help_text='Average judge score out of 100 against the idea rubric.')
    review_count = models.PositiveIntegerField(default=0)
    feedback = models.CharField(max_length=1000, blank=True, help_text="Reviewer's note shown to the team.")
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    reviewed_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"Idea for {self.team.name}: {self.title} ({self.status})"


class IdeaReview(TimeStampedModel):
    """One judge's scores for one idea: {criterion_id: score} against the idea rubric."""
    idea = models.ForeignKey(IdeaSubmission, on_delete=models.CASCADE, related_name='reviews')
    judge = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='hackathon_idea_reviews')
    scores = models.JSONField(default=dict)
    comment = models.TextField(blank=True)
    total = models.FloatField(default=0.0, help_text="This judge's weighted total out of 100.")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['idea', 'judge'], name='uniq_idea_review_per_judge'),
        ]
