import os
import sys
import django

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.feature_flags.models import FeatureFlag

DEFAULT_FLAGS = [
    {'key': 'events', 'name': 'Events Module', 'description': 'Enable workshops, seminars, and club meetups listing.'},
    {'key': 'hackathons', 'name': 'Hackathons Engine', 'description': 'Enable general hackathons engine and registration.'},
    {'key': 'iconcoders', 'name': 'IconCoders Flagship', 'description': 'Enable IconCoders annual flagship hackathon landing and Hall of Fame.'},
    {'key': 'codequest', 'name': 'Codequest Daily Problems', 'description': 'Enable daily problem of the day, streak tracking, and leaderboards.'},
    {'key': 'career', 'name': 'Career & Internships', 'description': 'Enable job drives, internship listings, and career applications.'},
    {'key': 'blogs', 'name': 'Blogs & Tutorials', 'description': 'Enable community tech blogs and tutorial write-ups.'},
]

def seed():
    for item in DEFAULT_FLAGS:
        flag, created = FeatureFlag.objects.get_or_create(
            key=item['key'],
            defaults={
                'name': item['name'],
                'description': item['description'],
                'is_enabled': True
            }
        )
        if created:
            print(f"Created Feature Flag: {flag.name}")
        else:
            print(f"Feature Flag already exists: {flag.name}")

if __name__ == '__main__':
    seed()
