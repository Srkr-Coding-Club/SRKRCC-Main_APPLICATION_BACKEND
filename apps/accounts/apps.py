from django.apps import AppConfig

class AccountsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.accounts'

    def ready(self):
        from django.db.models.signals import post_save, pre_save
        from apps.accounts.models import User
        from apps.accounts.services import club_id_registry

        # Every change of a Club ID, from any code path, is written to the registry history.
        pre_save.connect(lambda sender, instance, update_fields=None, **kw: club_id_registry.remember_previous(instance, update_fields), sender=User, weak=False, dispatch_uid='club_id_remember')
        post_save.connect(lambda sender, instance, created, update_fields=None, **kw: club_id_registry.log_change(instance, created, update_fields), sender=User, weak=False, dispatch_uid='club_id_log')
