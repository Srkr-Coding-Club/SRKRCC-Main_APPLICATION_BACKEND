from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from apps.forms.urls import members_urlpatterns

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/auth/', include('apps.accounts.urls')),
    path('api/feature-flags/', include('apps.feature_flags.urls')),
    path('api/forms/', include('apps.forms.urls')),
    path('api/members/', include((members_urlpatterns, 'members'))),
    path('api/events/', include('apps.events.urls')),
    path('api/hackathons/', include('apps.hackathons.urls')),
    path('api/codequest/', include('apps.codequest.urls')),
    path('api/career/', include('apps.career.urls')),
    path('api/blogs/', include('apps.blogs.urls')),
    path('api/audit/', include('apps.audit.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

