from django.db import models
from apps.accounts.models import User
from apps.accounts.services.club_id_service import ClubIDService


class ReferralService:
    """
    Resolves member referrals and manages club onboarding tracking for SRKR Coding Club.
    """

    @classmethod
    def resolve_referrer(cls, referrer_identifier: str | None) -> tuple[User | None, str, bool]:
        """
        Resolves a referrer string with confidence rules:
          Rule 1: Exact Club ID match (e.g. '25SCC100') -> high-confidence FK link
          Rule 2: Exact Email match -> high-confidence FK link
          Rule 3: Unique Exact Full Name match -> high-confidence FK link
          Rule 4: Multiple matching names -> returns (None, referrer_str, True) [Ambiguous]
          Rule 5: No match -> returns (None, referrer_str, False) [Raw string fallback]

        Returns: (resolved_user_or_none, raw_string, is_ambiguous)
        """
        if not referrer_identifier or not str(referrer_identifier).strip():
            return None, "", False

        cleaned = str(referrer_identifier).strip()

        # 1. Match Club ID
        if ClubIDService.validate_club_id_format(cleaned):
            user_by_cid = User.objects.filter(club_id__iexact=cleaned).first()
            if user_by_cid:
                return user_by_cid, cleaned, False

        # 2. Match Email
        if "@" in cleaned:
            user_by_email = User.objects.filter(email__iexact=cleaned.lower()).first()
            if user_by_email:
                return user_by_email, cleaned, False

        # 3. Match Full Name / First Name
        name_parts = cleaned.split()
        if len(name_parts) >= 1:
            q_filter = models.Q(first_name__iexact=cleaned) | models.Q(username__iexact=cleaned)
            if len(name_parts) >= 2:
                q_filter |= (
                    models.Q(first_name__iexact=name_parts[0]) &
                    models.Q(last_name__iexact=" ".join(name_parts[1:]))
                )

            matches = list(User.objects.filter(q_filter)[:5])
            if len(matches) == 1:
                return matches[0], cleaned, False
            elif len(matches) > 1:
                return None, cleaned, True  # Ambiguous match

        return None, cleaned, False

    @classmethod
    def get_referral_stats(cls, user: User) -> dict:
        """Returns the number and list of members referred by this user."""
        if not user or not user.is_authenticated:
            return {"referred_count": 0, "referred_members": []}

        referred_qs = user.referred_members.filter(is_active=True).values(
            "id", "email", "first_name", "last_name", "club_id", "branch", "registered_at"
        )
        return {
            "referred_count": referred_qs.count(),
            "referred_members": list(referred_qs),
        }
