"""
Team lifecycle emails for hackathons.

Participants hear about their team by email as well as in-app: when the team is
created, when it reaches the minimum size and becomes registered, when someone
joins or leaves, when an organizer removes someone, and when an organizer
changes the team's status.

Organizers can switch each email off and rewrite its subject and body per
hackathon (``Hackathon.team_email_config``). Texts use ``{{placeholders}}`` from a
fixed whitelist, so a template can never read anything beyond team basics.
Blank subject/body fall back to the built-in wording below.

Emails are sent one per recipient on a background thread after the database
transaction commits, so a slow or failing mail server never blocks or rolls back
a team change.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.html import escape

from .models import Hackathon, Team, TeamStatus

logger = logging.getLogger(__name__)

PLACEHOLDER_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
URL_PATTERN = re.compile(r"(https?://[^\s<]+)")

MAX_SUBJECT_LENGTH = 200
MAX_BODY_LENGTH = 5000


@dataclass(frozen=True)
class TeamEmailEvent:
    key: str
    label: str
    when: str
    recipients: str
    default_subject: str
    default_body: str


EVENTS: dict[str, TeamEmailEvent] = {e.key: e for e in (
    TeamEmailEvent(
        key="team_created",
        label="Team created",
        when="A participant creates a team that is still below the minimum size.",
        recipients="The team leader",
        default_subject="[{{hackathon_title}}] Your team {{team_name}} is created",
        default_body=(
            'Hi {{first_name}},\n\n'
            'Your team "{{team_name}}" has been created for {{hackathon_title}}.\n\n'
            '{{status_line}}\n\n'
            'Invite teammates and manage your team from your dashboard:\n{{dashboard_url}}'
        ),
    ),
    TeamEmailEvent(
        key="team_registered",
        label="Team registered",
        when="A team reaches the minimum team size and becomes registered.",
        recipients="Every team member",
        default_subject="[{{hackathon_title}}] Team {{team_name}} is registered",
        default_body=(
            'Hi {{first_name}},\n\n'
            'Your team "{{team_name}}" is now registered for {{hackathon_title}}.\n\n'
            'Team members:\n{{members_list}}\n\n'
            'Problem: {{problem_title}}\n\n'
            'View your team dashboard:\n{{dashboard_url}}'
        ),
    ),
    TeamEmailEvent(
        key="member_joined",
        label="Member joined",
        when="Someone accepts an invite or is added to the team by an organizer.",
        recipients="Every team member, including the new one",
        default_subject="[{{hackathon_title}}] {{member_name}} joined {{team_name}}",
        default_body=(
            'Hi {{first_name}},\n\n'
            '{{member_name}} joined team "{{team_name}}" for {{hackathon_title}}.\n\n'
            'Team members ({{team_size}} of {{max_team_size}}):\n{{members_list}}\n\n'
            '{{status_line}}\n\n'
            '{{dashboard_url}}'
        ),
    ),
    TeamEmailEvent(
        key="member_left",
        label="Member left",
        when="A member leaves the team, or the leader removes them.",
        recipients="The remaining team members",
        default_subject="[{{hackathon_title}}] {{member_name}} is no longer on {{team_name}}",
        default_body=(
            'Hi {{first_name}},\n\n'
            '{{member_name}} is no longer on team "{{team_name}}" for {{hackathon_title}}.\n\n'
            'Current members ({{team_size}} of {{max_team_size}}):\n{{members_list}}\n\n'
            '{{status_line}}\n\n'
            '{{dashboard_url}}'
        ),
    ),
    TeamEmailEvent(
        key="member_removed",
        label="You were removed",
        when="The team leader removes a member.",
        recipients="The removed member",
        default_subject="[{{hackathon_title}}] You were removed from {{team_name}}",
        default_body=(
            'Hi {{first_name}},\n\n'
            'You are no longer on team "{{team_name}}" for {{hackathon_title}}. '
            'You can join or create another team while registration is open.\n\n'
            '{{hackathon_url}}'
        ),
    ),
    TeamEmailEvent(
        key="status_changed",
        label="Status changed by organizers",
        when="An organizer changes the team's status (for example disqualified or withdrawn).",
        recipients="Every team member",
        default_subject="[{{hackathon_title}}] {{team_name}}: status updated",
        default_body=(
            'Hi {{first_name}},\n\n'
            'The status of your team "{{team_name}}" for {{hackathon_title}} was updated by the organizers.\n\n'
            '{{status_line}}\n\n'
            '{{dashboard_url}}'
        ),
    ),
)}

PLACEHOLDERS: dict[str, str] = {
    "first_name": "Recipient's first name",
    "full_name": "Recipient's full name",
    "team_name": "Team name",
    "hackathon_title": "Hackathon title",
    "leader_name": "Team leader's name",
    "member_name": "The person who joined, left or was removed (empty for other emails)",
    "members": "Team members, comma separated",
    "members_list": "Team members, one per line",
    "team_size": "Current number of members",
    "min_team_size": "Minimum team size",
    "max_team_size": "Maximum team size",
    "team_status": "Team status (Forming, Registered, Disqualified, Withdrawn)",
    "status_line": "One sentence describing where the team stands",
    "problem_title": "The team's chosen problem",
    "registration_closes_at": "When registration closes (blank if not set)",
    "dashboard_url": "Link to the team dashboard",
    "hackathon_url": "Link to the hackathon page",
}


class TeamEmailConfigError(ValueError):
    """The organizer's email settings are invalid; the message is shown to them."""


# ---------------------------------------------------------------------------
# Settings (what an organizer customises)
# ---------------------------------------------------------------------------

def _unknown_placeholders(text: str) -> list[str]:
    return sorted({key for key in PLACEHOLDER_PATTERN.findall(text) if key not in PLACEHOLDERS})


def normalize_config(raw) -> dict:
    """Validate organizer input and return the clean dict to store. Raises TeamEmailConfigError."""
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise TeamEmailConfigError("Email settings must be an object keyed by email type.")
    clean = {}
    for key, entry in raw.items():
        if key not in EVENTS:
            raise TeamEmailConfigError(f"Unknown email type '{key}'.")
        if not isinstance(entry, dict):
            raise TeamEmailConfigError(f"Settings for '{EVENTS[key].label}' must be an object.")
        label = EVENTS[key].label
        subject = " ".join(str(entry.get("subject") or "").split())
        body = str(entry.get("body") or "").replace("\r\n", "\n").strip()
        if len(subject) > MAX_SUBJECT_LENGTH:
            raise TeamEmailConfigError(f"'{label}': subject is too long (max {MAX_SUBJECT_LENGTH} characters).")
        if len(body) > MAX_BODY_LENGTH:
            raise TeamEmailConfigError(f"'{label}': message is too long (max {MAX_BODY_LENGTH} characters).")
        unknown = _unknown_placeholders(subject) + _unknown_placeholders(body)
        if unknown:
            names = ", ".join("{{%s}}" % name for name in sorted(set(unknown)))
            raise TeamEmailConfigError(f"'{label}': unknown placeholder {names}.")
        clean[key] = {"enabled": bool(entry.get("enabled", True)), "subject": subject, "body": body}
    return clean


def event_settings(hackathon: Hackathon, event_key: str) -> tuple[bool, str, str]:
    """(enabled, subject, body) with the built-in wording filling anything left blank."""
    event = EVENTS[event_key]
    entry = (hackathon.team_email_config or {}).get(event_key) or {}
    return (
        bool(entry.get("enabled", True)),
        entry.get("subject") or event.default_subject,
        entry.get("body") or event.default_body,
    )


def describe_settings(hackathon: Hackathon) -> list[dict]:
    """Everything the admin screen needs to show and edit each email."""
    items = []
    for key, event in EVENTS.items():
        entry = (hackathon.team_email_config or {}).get(key) or {}
        items.append({
            "key": key,
            "label": event.label,
            "when": event.when,
            "recipients": event.recipients,
            "enabled": bool(entry.get("enabled", True)),
            "subject": entry.get("subject", ""),
            "body": entry.get("body", ""),
            "default_subject": event.default_subject,
            "default_body": event.default_body,
        })
    return items


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_subject(template: str, context: dict) -> str:
    text = PLACEHOLDER_PATTERN.sub(lambda m: str(context.get(m.group(1), "")), template)
    return " ".join(text.split())[:MAX_SUBJECT_LENGTH]


def render_body(template: str, context: dict) -> tuple[str, str]:
    """Return (plain text, html). Values are HTML-escaped; only plain URLs become links."""
    text = PLACEHOLDER_PATTERN.sub(lambda m: str(context.get(m.group(1), "")), template).strip()
    html_source = PLACEHOLDER_PATTERN.sub(lambda m: escape(str(context.get(m.group(1), ""))), escape(template)).strip()
    html_source = URL_PATTERN.sub(r'<a href="\1">\1</a>', html_source)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", html_source) if p.strip()]
    html = "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs)
    return text, html


def _status_line(team: Team, size: int) -> str:
    hackathon = team.hackathon
    if team.status == TeamStatus.REGISTERED:
        return f"Your team has {size} member{'s' if size != 1 else ''} and is registered."
    if team.status == TeamStatus.FORMING:
        needed = max(hackathon.min_team_size - size, 1)
        return (
            f"Your team has {size} of the minimum {hackathon.min_team_size} members, so it is not registered yet. "
            f"Invite {needed} more member{'s' if needed != 1 else ''} to complete registration."
        )
    if team.status == TeamStatus.DISQUALIFIED:
        return "Your team was disqualified by the organizers. Contact them if you think this is a mistake."
    if team.status == TeamStatus.WITHDRAWN:
        return "Your team was marked as withdrawn."
    return f"Your team status is {team.get_status_display()}."


def _display_name(user) -> str:
    return f"{user.first_name} {user.last_name}".strip() or user.username or user.email.split("@")[0]


def build_context(team: Team, recipient, member=None) -> dict:
    hackathon = team.hackathon
    members = [m.user for m in team.memberships.select_related("user").order_by("joined_at", "id")]
    names = [_display_name(u) for u in members]
    base = getattr(settings, "FRONTEND_URL", "http://localhost:3000").rstrip("/")
    closes = ""
    if hackathon.registration_closes_at:
        closes = timezone.localtime(hackathon.registration_closes_at).strftime("%d %b %Y, %I:%M %p")
    if team.problem_statement_id:
        problem = team.problem_statement.title
    else:
        problem = team.custom_problem_title or "Not chosen yet"
    return {
        "first_name": recipient.first_name or recipient.email.split("@")[0],
        "full_name": _display_name(recipient),
        "team_name": team.name,
        "hackathon_title": hackathon.title,
        "leader_name": _display_name(team.leader) if team.leader else "",
        "member_name": _display_name(member) if member else "",
        "members": ", ".join(names),
        "members_list": "\n".join(f"- {name}" for name in names),
        "team_size": len(members),
        "min_team_size": hackathon.min_team_size,
        "max_team_size": hackathon.max_team_size,
        "team_status": team.get_status_display(),
        "status_line": _status_line(team, len(members)),
        "problem_title": problem,
        "registration_closes_at": closes,
        "dashboard_url": f"{base}/hackathons/{hackathon.slug}/dashboard",
        "hackathon_url": f"{base}/hackathons/{hackathon.slug}",
    }


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def _recipients(event_key: str, team: Team, member) -> list:
    members = [m.user for m in team.memberships.select_related("user")]
    if event_key == "team_created":
        return [team.leader] if team.leader else []
    if event_key == "member_removed":
        return [member] if member else []
    if event_key == "member_left":
        return [u for u in members if member is None or u.pk != member.pk]
    return members


def _send_one(hackathon: Hackathon, event_key: str, team: Team, recipient, member) -> bool:
    from apps.core.services.email_service import EmailNotificationService

    _, subject_template, body_template = event_settings(hackathon, event_key)
    context = build_context(team, recipient, member)
    subject = render_subject(subject_template, context)
    text, html = render_body(body_template, context)
    return EmailNotificationService.send_email(recipient.email, subject, text, html)


def dispatch(event_key: str, team_id: int, member_id: int | None = None) -> int:
    """Send one team email to each recipient. Runs on a background thread; returns how many were sent."""
    from django.contrib.auth import get_user_model

    team = Team.objects.select_related("hackathon", "leader", "problem_statement").filter(pk=team_id).first()
    if team is None:
        return 0
    enabled, _, _ = event_settings(team.hackathon, event_key)
    if not enabled:
        return 0
    member = get_user_model().objects.filter(pk=member_id).first() if member_id else None
    sent = 0
    for recipient in _recipients(event_key, team, member):
        if not recipient.email:
            continue
        try:
            sent += int(_send_one(team.hackathon, event_key, team, recipient, member))
        except Exception:
            logger.exception("Team email %s to user %s failed", event_key, recipient.pk)
    return sent


def notify_team(event_key: str, team: Team, member=None) -> None:
    """Queue a team email for after the surrounding transaction commits."""
    from apps.core.tasks import run_in_background

    team_id, member_id = team.pk, member.pk if member else None
    transaction.on_commit(lambda: run_in_background(lambda: dispatch(event_key, team_id, member_id)))


def send_test(hackathon: Hackathon, event_key: str, to_user, subject_template: str, body_template: str) -> bool:
    """Render the (possibly unsaved) texts with sample team data and email them to one admin."""
    from apps.core.services.email_service import EmailNotificationService

    sample = sample_context(hackathon, to_user)
    subject = "[TEST] " + render_subject(subject_template, sample)
    text, html = render_body(body_template, sample)
    return EmailNotificationService.send_email(to_user.email, subject, text, html)


def sample_context(hackathon: Hackathon, user) -> dict:
    base = getattr(settings, "FRONTEND_URL", "http://localhost:3000").rstrip("/")
    names = [_display_name(user), "Asha Rao", "Ravi Kumar"]
    return {
        "first_name": user.first_name or user.email.split("@")[0],
        "full_name": _display_name(user),
        "team_name": "Sample Squad",
        "hackathon_title": hackathon.title,
        "leader_name": names[0],
        "member_name": "Ravi Kumar",
        "members": ", ".join(names),
        "members_list": "\n".join(f"- {n}" for n in names),
        "team_size": len(names),
        "min_team_size": hackathon.min_team_size,
        "max_team_size": hackathon.max_team_size,
        "team_status": "Registered",
        "status_line": f"Your team has {len(names)} members and is registered.",
        "problem_title": "Sample problem statement",
        "registration_closes_at": "",
        "dashboard_url": f"{base}/hackathons/{hackathon.slug}/dashboard",
        "hackathon_url": f"{base}/hackathons/{hackathon.slug}",
    }
