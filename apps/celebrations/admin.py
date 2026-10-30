from django.contrib import admin

from .models import Celebration, CelebrationSetting


@admin.register(Celebration)
class CelebrationAdmin(admin.ModelAdmin):
    list_display = ('id', 'kind', 'user', 'size', 'seen_at', 'expires_at', 'created_at')
    list_filter = ('kind', 'size')
    search_fields = ('key', 'title', 'user__email')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(CelebrationSetting)
class CelebrationSettingAdmin(admin.ModelAdmin):
    list_display = ('user', 'enabled')
    search_fields = ('user__email',)
