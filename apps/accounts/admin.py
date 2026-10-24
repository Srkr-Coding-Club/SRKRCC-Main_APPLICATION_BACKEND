from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from datetime import timedelta

from django.utils import timezone

from .models import User
from .services import email_verification

@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ['username', 'email', 'role', 'roll_number', 'branch', 'year', 'is_staff', 'email_verified_at', 'email_verify_deadline']
    list_filter = ['role', 'is_staff', 'is_superuser', 'branch', 'year']
    actions = ['mark_email_verified', 'extend_verification_7_days']
    fieldsets = BaseUserAdmin.fieldsets + (
        ('Platform Details', {'fields': ('role', 'roll_number', 'branch', 'year', 'phone_number', 'github_profile', 'linkedin_profile')}),
        ('Email verification', {'fields': ('email_verified_at', 'email_verify_deadline', 'email_disputed_at')}),
    )

    @admin.action(description='Mark email as verified (use when the person cannot receive the link)')
    def mark_email_verified(self, request, queryset):
        done = sum(email_verification.mark_verified(user, via=f'django-admin:{request.user.pk}') for user in queryset)
        self.message_user(request, f'{done} account(s) marked verified.')

    @admin.action(description='Give 7 more days to verify')
    def extend_verification_7_days(self, request, queryset):
        for user in queryset.filter(email_verified_at__isnull=True):
            base = max(email_verification.deadline(user), timezone.now())
            User.objects.filter(pk=user.pk).update(email_verify_deadline=base + timedelta(days=7))
        self.message_user(request, 'Deadlines extended by 7 days.')
