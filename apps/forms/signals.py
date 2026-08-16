from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Response
from apps.audit.models import AuditLog

@receiver(post_save, sender=Response)
def on_response_created(sender, instance, created, **kwargs):
    if created and not instance.is_test_submission:
        actor_user = instance.user or instance.created_by_admin
        AuditLog.objects.create(
            actor=actor_user,
            action="Submitted Response",
            target_model="Form",
            target_id=str(instance.form.id),
            details={
                "form_title": instance.form.title,
                "response_id": instance.id,
                "form_version": instance.form_version,
                "is_manual_entry": instance.is_manual_entry,
            }
        )
