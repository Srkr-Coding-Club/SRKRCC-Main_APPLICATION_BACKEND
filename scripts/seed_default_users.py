import os
import sys
import django

# Setup Django environment
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.accounts.models import User, UserRole

DEFAULT_USERS = [
    {
        'email': 'admin@srkr.ac.in',
        'username': 'admin',
        'password': 'Admin@123',
        'first_name': 'Admin',
        'last_name': 'User',
        'role': UserRole.ADMIN,
        'is_staff': True,
        'is_superuser': True,
        'branch': 'CSE',
        'year': 4,
        'roll_number': 'ADMIN-001',
    },
    {
        'email': 'clublead@srkr.ac.in',
        'username': 'clublead',
        'password': 'ClubLead@123',
        'first_name': 'Club',
        'last_name': 'Lead',
        'role': UserRole.CLUB_LEAD,
        'is_staff': True,
        'is_superuser': False,
        'branch': 'CSE',
        'year': 4,
        'roll_number': 'LEAD-001',
    },
    {
        'email': 'judge@srkr.ac.in',
        'username': 'judge',
        'password': 'Judge@123',
        'first_name': 'Hackathon',
        'last_name': 'Judge',
        'role': UserRole.JUDGE,
        'is_staff': False,
        'is_superuser': False,
        'branch': 'IT',
        'year': 4,
        'roll_number': 'JUDGE-001',
    },
    {
        'email': 'member@srkr.ac.in',
        'username': 'member',
        'password': 'Member@123',
        'first_name': 'Student',
        'last_name': 'Member',
        'role': UserRole.MEMBER,
        'is_staff': False,
        'is_superuser': False,
        'branch': 'CSE',
        'year': 2,
        'roll_number': '23B91A0501',
    },
]

def seed_users():
    print("=" * 60)
    print("SRKRCC Platform — Default Users & Credentials Seeder")
    print("=" * 60)

    for user_data in DEFAULT_USERS:
        email = user_data['email']
        password = user_data['password']
        
        user, created = User.objects.get_or_select = User.objects.get_or_create(
            email=email,
            defaults={
                'username': user_data['username'],
                'first_name': user_data['first_name'],
                'last_name': user_data['last_name'],
                'role': user_data['role'],
                'is_staff': user_data['is_staff'],
                'is_superuser': user_data['is_superuser'],
                'branch': user_data['branch'],
                'year': user_data['year'],
                'roll_number': user_data['roll_number'],
            }
        )

        user.set_password(password)
        user.role = user_data['role']
        user.is_staff = user_data['is_staff']
        user.is_superuser = user_data['is_superuser']
        user.save()

        action = "Created" if created else "Updated & Reset"
    # Seed initial audit logs
    from apps.audit.models import AuditLog
    admin_user = User.objects.filter(email='admin@srkr.ac.in').first()
    if not AuditLog.objects.exists():
        AuditLog.objects.create(
            actor=admin_user,
            action="System Initialized",
            target_model="Platform",
            target_id="1",
            details={"status": "Online", "version": "2.0.0"}
        )
        AuditLog.objects.create(
            actor=admin_user,
            action="Toggled Feature Flag",
            target_model="FeatureFlag",
            target_id="module_hackathons",
            details={"status": "ENABLED", "environment": "production"}
        )
        AuditLog.objects.create(
            actor=admin_user,
            action="Published Dynamic Form",
            target_model="Form",
            target_id="iconcoders-2025-registration",
            details={"title": "IconCoders Flagship Hackathon 2025", "status": "PUBLISHED"}
        )
        print("[Created] Seeded initial platform audit logs.")

    print("=" * 60)
    print("Done! All default credentials and audit logs are ready for use.")

if __name__ == '__main__':
    seed_users()

