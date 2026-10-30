"""
Every kind of celebration the application can raise: how big it is and what the card says by default.

The text may use ``{placeholders}`` filled from the context given to ``services.award``; one that is missing is left
empty rather than failing the action that caused the celebration. Add a kind here before awarding it.
"""
from __future__ import annotations

import string
from dataclasses import dataclass

from .models import CelebrationSize

SMALL, MEDIUM, BIG = CelebrationSize.SMALL, CelebrationSize.MEDIUM, CelebrationSize.BIG


@dataclass(frozen=True)
class Kind:
    size: str
    title: str
    message: str = ''


KINDS: dict[str, Kind] = {
    # The account
    'account.welcome': Kind(MEDIUM, 'Welcome to SRKR Coding Club!', 'Your account is ready. Look around: events, forms, hackathons and CodeQuest are all here.'),
    'account.email_verified': Kind(SMALL, 'Email verified', 'Thank you. Everything on the site is open to you.'),
    'account.profile_completed': Kind(SMALL, 'Profile complete', 'Your profile is ready, so you can register for anything.'),
    'account.club_member': Kind(MEDIUM, 'You are a club member!', 'Your club ID is {club_id}. Welcome to the family.'),
    # Events
    'event.registered': Kind(MEDIUM, "You're registered!", 'You are registered for {event}. See you there.'),
    'event.checked_in': Kind(SMALL, "You're checked in", 'Welcome to {event}.'),
    'event.badge_earned': Kind(MEDIUM, 'New badge: {badge}', 'You earned it by showing up. Keep going.'),
    # Hackathons
    'hackathon.team_registered': Kind(BIG, "You're in!", '{team} is registered for {hackathon}. Time to build something great.'),
    'hackathon.waitlist_promoted': Kind(BIG, 'A place opened up!', '{team} is now registered for {hackathon}.'),
    'hackathon.member_joined': Kind(SMALL, '{member} joined {team}', 'Your team is growing.'),
    'hackathon.joined_team': Kind(MEDIUM, 'Welcome to {team}!', 'You are part of the team for {hackathon}.'),
    'hackathon.idea_approved': Kind(MEDIUM, 'Your idea was approved', '{team}: the organizers liked it.'),
    'hackathon.project_submitted': Kind(MEDIUM, 'Project submitted!', '{team} has handed in its project for {hackathon}.'),
    'hackathon.shortlisted': Kind(BIG, "You're shortlisted!", '{team} goes through {round}.'),
    'hackathon.winner': Kind(BIG, 'Congratulations!', '{team} won {award} at {hackathon}.'),
    'hackathon.certificate_ready': Kind(MEDIUM, 'Your certificate is ready', 'Your certificate for {hackathon} can be downloaded now.'),
    'hackathon.checked_in': Kind(SMALL, "You're checked in", 'Welcome to {hackathon}.'),
    # CodeQuest
    'codequest.streak': Kind(MEDIUM, '{days}-day streak!', 'You have solved something {days} days in a row.'),
    'codequest.first_solve': Kind(SMALL, 'First problem solved', 'The first of many.'),
    # Anything an organizer wants to celebrate by hand; the caller gives the words.
    'custom': Kind(MEDIUM, '{title}', '{message}'),
}


class _Blank(dict):
    def __missing__(self, key):
        return ''


def render(text: str, context: dict) -> str:
    """Fill a template; a placeholder with no value becomes empty."""
    return string.Formatter().vformat(text, (), _Blank({k: ('' if v is None else v) for k, v in context.items()})).strip()
