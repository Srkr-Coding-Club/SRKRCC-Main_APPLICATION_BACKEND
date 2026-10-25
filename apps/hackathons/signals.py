import logging

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(post_save, sender=get_user_model())
def claim_hackathon_email_invites(sender, instance, created, **kwargs):
    """A brand-new account picks up any team invites that were emailed to its address before it existed."""
    if not (created and instance.email):
        return

    def claim():
        from apps.hackathons.services import TeamService
        try:
            TeamService.claim_email_invites(instance)
        except Exception:
            logger.exception('Could not claim hackathon email invites for user %s', instance.pk)

    transaction.on_commit(claim)


from django.db.models.signals import pre_delete  # noqa: E402

from .models import Hackathon, Team  # noqa: E402


@receiver(pre_delete, sender=Team)
def release_team_idea_files(sender, instance, **kwargs):
    """Deleting a team removes its idea files from storage after the delete commits."""
    from . import idea_files
    idea_files.release_all(instance.idea_files.all())


@receiver(pre_delete, sender=Hackathon)
def release_hackathon_idea_files(sender, instance, **kwargs):
    from . import idea_files
    idea_files.release_all(instance.idea_files.all())
