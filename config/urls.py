from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.http import JsonResponse
from django.utils import timezone
from apps.forms.urls import members_urlpatterns

def ping_view(request):
    return JsonResponse({
        "status": "ok",
        "service": "srkrcc-backend",
        "timestamp": timezone.now().isoformat(),
    })

urlpatterns = [
    path('', ping_view, name='root'),
    path('api/ping/', ping_view, name='ping'),
    path('api/ping', ping_view),
    path('admin/', admin.site.urls),
    path('api/auth/', include('apps.accounts.urls')),
    path('api/feature-flags/', include('apps.feature_flags.urls')),
    path('api/forms/', include('apps.forms.urls')),
    path('api/members/', include((members_urlpatterns, 'members'))),
    path('api/', include('apps.attendance.urls')),
    path('api/events/', include('apps.events.urls')),
    path('api/hackathons/', include('apps.hackathons.urls')),
    path('api/codequest/', include('apps.codequest.urls')),
    path('api/career/', include('apps.career.urls')),
    path('api/blogs/', include('apps.blogs.urls')),
    path('api/audit/', include('apps.audit.urls')),
    path('api/admin/dmc/', include('apps.core.dmc.urls')),
    path('api/admin/backups/', include('apps.core.urls_backup')),
    path('api/admin/jobs/', include('apps.core.urls_jobs')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

