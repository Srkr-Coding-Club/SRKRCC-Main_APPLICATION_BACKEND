import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.accounts.models import User
from apps.events.models import Event
from apps.hackathons.models import Hackathon
from apps.forms.models import Form
from apps.codequest.models import Problem
from apps.career.models import JobListing
from apps.blogs.models import BlogPost
from apps.feature_flags.models import FeatureFlag
from apps.audit.models import AuditLog

def clean(val):
    return str(val).encode('ascii', 'ignore').decode('ascii')

print("--- DATABASE TABLE RECORD COUNTS ---")
print("Users:", User.objects.count())
for u in User.objects.all():
    print(f"  - {clean(u.email)} ({clean(u.role)})")

print("Events:", Event.objects.count())
for e in Event.objects.all():
    print(f"  - {clean(e.title)}")

print("Hackathons:", Hackathon.objects.count())
for h in Hackathon.objects.all():
    print(f"  - {clean(h.title)} (Prize: {clean(h.prize_pool)})")

print("Forms:", Form.objects.count())
for f in Form.objects.all():
    print(f"  - {clean(f.title)} (Status: {clean(f.status)})")

print("Problems:", Problem.objects.count())
for p in Problem.objects.all():
    print(f"  - {clean(p.title)}")

print("JobListings:", JobListing.objects.count())
for j in JobListing.objects.all():
    print(f"  - {clean(j.title)}")

print("Blogs:", BlogPost.objects.count())
for b in BlogPost.objects.all():
    print(f"  - {clean(b.title)}")

print("FeatureFlags:", FeatureFlag.objects.count())
for ff in FeatureFlag.objects.all():
    print(f"  - {clean(ff.name)}: {ff.is_enabled}")

print("AuditLogs:", AuditLog.objects.count())
for al in AuditLog.objects.all():
    print(f"  - {clean(al.action)} on {clean(al.target_model)}")
