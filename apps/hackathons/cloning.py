"""Clone a hackathon as the next edition: same setup, new dates, no people."""
from __future__ import annotations


from django.db import transaction
from django.utils.text import slugify

from apps.audit.utils import log_audit_event

from .errors import HackathonError
from .models import Hackathon, HackathonStage, HackathonStatus, JudgingCriterion, ProblemStatement, Round

# Settings copied as they are; dates are handled separately because they move with the new start.
COPIED_FIELDS = (
    'theme', 'description', 'prize_pool', 'banner_image', 'banner_crop', 'mode', 'venue', 'organizer_name', 'logo_image',
    'tags', 'prizes', 'problem_domains', 'min_team_size', 'max_team_size', 'allow_open_innovation', 'required_profile_fields',
    'faq', 'team_email_config', 'features', 'max_teams', 'max_participants', 'waitlist_enabled', 'eligibility', 'rules',
    'require_rules_acceptance', 'registration_questions', 'show_leaderboard', 'mentors', 'judges', 'sponsors', 'contacts',
    'community_links', 'venue_map_url', 'is_flagship', 'idea_review_required', 'project_requires_approved_idea',
    'payment_stage', 'idea_fee_amount', 'idea_payment_note', 'idea_deck_required', 'idea_linkedin_required', 'idea_edits_enabled',
    'check_in_days', 'check_in_slots_per_day',
)
SHIFTED_FIELDS = (
    'start_date', 'end_date', 'registration_opens_at', 'registration_closes_at', 'submission_opens_at',
    'submission_deadline', 'problem_statements_reveal_at', 'idea_opens_at', 'idea_deadline',
    'check_in_opens_at', 'check_in_closes_at',
)


class CloneService:

    @staticmethod
    @transaction.atomic
    def clone(source: Hackathon, actor, *, title: str, slug: str, start_date, copy_problem_statements: bool = False) -> Hackathon:
        title = ' '.join((title or '').split())
        slug = slugify(slug or title)
        if not title or not slug:
            raise HackathonError('Give the new hackathon a title.', 'INVALID', field='title')
        if slug in {'my-teams', 'my-invites', 'teams', 'invites', 'submissions', 'certificates'}:
            raise HackathonError('That slug is reserved. Choose another.', 'INVALID', field='slug')
        if Hackathon.objects.filter(slug=slug).exists():
            raise HackathonError('A hackathon with that slug already exists.', 'SLUG_TAKEN', field='slug')
        delta = start_date - source.start_date

        values = {name: getattr(source, name) for name in COPIED_FIELDS}
        for name in SHIFTED_FIELDS:
            original = getattr(source, name)
            values[name] = original + delta if original else None
        clone = Hackathon.objects.create(title=title, slug=slug, status=HackathonStatus.DRAFT, **values)

        for criterion in source.criteria.all():
            JudgingCriterion.objects.create(
                hackathon=clone, target=criterion.target, name=criterion.name, description=criterion.description,
                max_score=criterion.max_score, weight=criterion.weight, order=criterion.order,
            )
        for stage in source.stages.all():
            HackathonStage.objects.create(
                hackathon=clone, title=stage.title, description=stage.description, badge=stage.badge, location=stage.location,
                tone=stage.tone, marker=stage.marker, icon=stage.icon, link_label=stage.link_label, link_url=stage.link_url,
                is_visible=stage.is_visible, order=stage.order,
                starts_at=stage.starts_at + delta if stage.starts_at else None,
                ends_at=stage.ends_at + delta if stage.ends_at else None,
            )
        for round_obj in source.rounds.all():
            Round.objects.create(
                hackathon=clone, order=round_obj.order, name=round_obj.name, description=round_obj.description,
                kind=round_obj.kind, shortlist_limit=round_obj.shortlist_limit,
                starts_at=round_obj.starts_at + delta if round_obj.starts_at else None,
                ends_at=round_obj.ends_at + delta if round_obj.ends_at else None,
            )
        if copy_problem_statements:
            for statement in source.problem_statements.all():
                ProblemStatement.objects.create(
                    hackathon=clone, code=statement.code, title=statement.title, description=statement.description,
                    domain=statement.domain, tags=statement.tags, max_teams=statement.max_teams,
                    is_active=statement.is_active, order=statement.order,
                )
        log_audit_event(
            actor=actor, action='Cloned Hackathon', target_model='Hackathon', target_id=clone.slug,
            details={'source': source.slug, 'shifted_days': delta.days},
        )
        return clone
