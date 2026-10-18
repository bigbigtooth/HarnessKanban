from django.apps import AppConfig


class TasksConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tasks"
    label = "tasks"

    def ready(self) -> None:
        from django.db.models.signals import post_migrate

        from apps.tasks.claim_lock import restore_claim_lock_after_migrate

        post_migrate.connect(restore_claim_lock_after_migrate, sender=self)
