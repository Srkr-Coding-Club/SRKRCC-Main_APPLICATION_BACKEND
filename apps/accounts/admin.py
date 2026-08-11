from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User

@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ['username', 'email', 'role', 'roll_number', 'branch', 'year', 'is_staff']
    list_filter = ['role', 'is_staff', 'is_superuser', 'branch', 'year']
    fieldsets = BaseUserAdmin.fieldsets + (
        ('Platform Details', {'fields': ('role', 'roll_number', 'branch', 'year', 'phone_number', 'github_profile', 'linkedin_profile')}),
    )
