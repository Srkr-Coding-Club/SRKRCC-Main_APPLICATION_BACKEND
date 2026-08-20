import os
import sys
from datetime import date, timedelta
import django

# Setup Django environment
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.utils import timezone
from apps.accounts.models import User, UserRole
from apps.forms.models import Form, FormField, Response, Answer
from apps.codequest.models import Problem, Submission as CodeSubmission, UserStreak, Difficulty
from apps.events.models import Event
from apps.hackathons.models import Hackathon, Team
from apps.feature_flags.models import FeatureFlag
from apps.audit.models import AuditLog

def seed_database():
    print("=" * 70)
    print("SRKRCC Platform — Master Database Seeder (Live DB Population)")
    print("=" * 70)

    # 1. Seed Core Platform Users
    users_data = [
        {
            'email': 'admin@srkr.ac.in',
            'username': 'admin',
            'password': 'Admin@123',
            'first_name': 'System',
            'last_name': 'Administrator',
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
            'first_name': 'Ananya',
            'last_name': 'Verma',
            'role': UserRole.CLUB_LEAD,
            'is_staff': True,
            'is_superuser': False,
            'branch': 'CSE',
            'year': 4,
            'roll_number': '21B91A0502',
        },
        {
            'email': 'judge@srkr.ac.in',
            'username': 'judge',
            'password': 'Judge@123',
            'first_name': 'Dr. K. S.',
            'last_name': 'Murthy',
            'role': UserRole.JUDGE,
            'is_staff': False,
            'is_superuser': False,
            'branch': 'IT',
            'year': 4,
            'roll_number': 'FACULTY-001',
        },
        {
            'email': 'rahul.sharma@srkr.ac.in',
            'username': 'rahulsharma',
            'password': 'Member@123',
            'first_name': 'Rahul',
            'last_name': 'Sharma',
            'role': UserRole.MEMBER,
            'is_staff': False,
            'is_superuser': False,
            'branch': 'Computer Science & Engineering',
            'year': 3,
            'roll_number': '22B91A0501',
        },
        {
            'email': 'member@srkr.ac.in',
            'username': 'member',
            'password': 'Member@123',
            'first_name': 'Kiran',
            'last_name': 'Kumar',
            'role': UserRole.MEMBER,
            'is_staff': False,
            'is_superuser': False,
            'branch': 'Electronics & Communication',
            'year': 2,
            'roll_number': '23B91A0415',
        },
    ]

    users_map = {}
    for u_info in users_data:
        user, _ = User.objects.get_or_create(email=u_info['email'])
        user.username = u_info['username']
        user.first_name = u_info['first_name']
        user.last_name = u_info['last_name']
        user.role = u_info['role']
        user.is_staff = u_info['is_staff']
        user.is_superuser = u_info['is_superuser']
        user.branch = u_info['branch']
        user.year = u_info['year']
        user.roll_number = u_info['roll_number']
        user.set_password(u_info['password'])
        user.save()
        users_map[u_info['email']] = user
        print(f" [User] {user.email:25} | Role: {user.role:10} | Name: {user.get_full_name()}")

    rahul = users_map['rahul.sharma@srkr.ac.in']
    admin = users_map['admin@srkr.ac.in']

    # 2. Seed CodeQuest Problems & User Streak
    today = date.today()
    prob1, _ = Problem.objects.get_or_create(
        slug='two-sum-srkr-arena',
        defaults={
            'title': 'Two Sum Algorithm Challenge',
            'difficulty': Difficulty.EASY,
            'statement': 'Given an array of integers nums and an integer target, return indices of the two numbers such that they add up to target.',
            'scheduled_date': today,
            'tags': ['Arrays', 'Hash Table', 'DSA'],
        }
    )

    prob2, _ = Problem.objects.get_or_create(
        slug='binary-tree-maximum-path-sum',
        defaults={
            'title': 'Binary Tree Maximum Path Sum',
            'difficulty': Difficulty.HARD,
            'statement': 'Find the maximum path sum of any non-empty path in a binary tree.',
            'scheduled_date': today - timedelta(days=1),
            'tags': ['Trees', 'DFS', 'Dynamic Programming'],
        }
    )

    # Seed User Streak & Submissions for Rahul Sharma
    streak, _ = UserStreak.objects.get_or_create(user=rahul)
    streak.current_streak = 14
    streak.max_streak = 21
    streak.last_solved_date = today
    streak.save()

    CodeSubmission.objects.get_or_create(
        problem=prob1,
        user=rahul,
        defaults={'code': 'def twoSum(nums, target): return [0, 1]', 'is_correct': True, 'language': 'python'}
    )
    CodeSubmission.objects.get_or_create(
        problem=prob2,
        user=rahul,
        defaults={'code': 'def maxPathSum(root): return 42', 'is_correct': True, 'language': 'python'}
    )
    print(" [CodeQuest] Seeded problems & 14-day streak for Rahul Sharma.")

    # 3. Seed Dynamic Forms & Field Schemas
    forms_data = [
        {
            'title': 'IconCoders Flagship Hackathon 2025',
            'slug': 'iconcoders-2025-registration',
            'description': 'Premier 48-hour competitive sprint & algorithmic hackathon at SRKR Engineering College.',
            'category': 'AI/ML & GenAI',
            'status': 'PUBLISHED',
            'open_at': timezone.now() - timedelta(days=5),
            'close_at': timezone.now() + timedelta(days=25),
            'fields': [
                {'label': 'Team Name', 'type': 'TEXT', 'is_required': True, 'placeholder': 'e.g. ByteCraft', 'order': 1},
                {'label': 'Team Leader Email', 'type': 'EMAIL', 'is_required': True, 'placeholder': 'leader@srkr.ac.in', 'order': 2},
                {'label': 'Hackathon Track', 'type': 'DROPDOWN', 'is_required': True, 'options': ['AI/ML & GenAI', 'Full Stack Web3', 'Cybersecurity', 'Open Innovation'], 'order': 3},
                {'label': 'Team Size', 'type': 'RADIO', 'is_required': True, 'options': ['2 Members', '3 Members', '4 Members'], 'order': 4},
                {'label': 'Project Repository URL', 'type': 'TEXT', 'is_required': False, 'placeholder': 'https://github.com/...', 'order': 5},
            ]
        },
        {
            'title': 'Hands-on Web Dev & Next.js Workshop',
            'slug': 'hands-on-nextjs-workshop-2025',
            'description': 'Interactive masterclass on React Server Components, Tailwind CSS, and Full-Stack APIs.',
            'category': 'Frontend Engineering',
            'status': 'PUBLISHED',
            'open_at': timezone.now() - timedelta(days=2),
            'close_at': timezone.now() + timedelta(days=10),
            'fields': [
                {'label': 'Full Name', 'type': 'TEXT', 'is_required': True, 'placeholder': 'Enter your name', 'order': 1},
                {'label': 'College Email', 'type': 'EMAIL', 'is_required': True, 'placeholder': 'student@srkr.ac.in', 'order': 2},
                {'label': 'Prior React Experience', 'type': 'RADIO', 'is_required': True, 'options': ['Beginner', 'Intermediate', 'Advanced'], 'order': 3},
                {'label': 'GitHub Profile', 'type': 'TEXT', 'is_required': False, 'placeholder': 'https://github.com/username', 'order': 4},
            ]
        },
        {
            'title': 'Codequest Daily Algorithm Challenge',
            'slug': 'codequest-daily-algorithm-challenge',
            'description': 'Daily competitive programming challenge to sharpen data structures and algorithmic thinking.',
            'category': 'Data Structures & Algorithms',
            'status': 'PUBLISHED',
            'open_at': timezone.now() - timedelta(days=14),
            'close_at': timezone.now() + timedelta(days=90),
            'fields': [
                {'label': 'Participant Name', 'type': 'TEXT', 'is_required': True, 'placeholder': 'Your full name', 'order': 1},
                {'label': 'Preferred Programming Language', 'type': 'DROPDOWN', 'is_required': True, 'options': ['Python', 'C++', 'Java', 'TypeScript'], 'order': 2},
            ]
        },
        {
            'title': 'Club Core Team & Volunteer Recruitment',
            'slug': 'club-core-team-recruitment-2025',
            'description': 'Apply to become an executive coordinator, designer, or technical mentor in SRKR Coding Club.',
            'category': 'Club Operations',
            'status': 'DRAFT',
            'open_at': None,
            'close_at': None,
            'fields': [
                {'label': 'Candidate Name', 'type': 'TEXT', 'is_required': True, 'order': 1},
                {'label': 'Interested Department', 'type': 'DROPDOWN', 'is_required': True, 'options': ['Technical Lead', 'Event Management', 'UI/UX Design', 'PR & Media'], 'order': 2},
            ]
        }
    ]

    forms_map = {}
    for f_data in forms_data:
        fields = f_data.pop('fields')
        form, _ = Form.objects.get_or_create(slug=f_data['slug'], defaults=f_data)
        form.title = f_data['title']
        form.description = f_data['description']
        form.category = f_data['category']
        form.status = f_data['status']
        form.open_at = f_data['open_at']
        form.close_at = f_data['close_at']
        form.save()
        forms_map[form.slug] = form

        for field_info in fields:
            FormField.objects.get_or_create(
                form=form,
                label=field_info['label'],
                defaults=field_info
            )
        print(f" [Form] {form.title:40} | Status: {form.status:10}")

    # 4. Seed Live Form Responses & Answers for Rahul Sharma & Other Users
    f1 = forms_map['iconcoders-2025-registration']
    f2 = forms_map['hands-on-nextjs-workshop-2025']
    f3 = forms_map['codequest-daily-algorithm-challenge']

    # Response 1: Rahul registered for IconCoders
    resp1, _ = Response.objects.get_or_create(
        form=f1,
        user=rahul,
        defaults={'form_version': 1}
    )
    for field in f1.fields.all():
        val = 'ByteMasters' if 'Team Name' in field.label else (
            'rahul.sharma@srkr.ac.in' if 'Email' in field.label else (
                'AI/ML & GenAI' if 'Track' in field.label else '4 Members'
            )
        )
        Answer.objects.get_or_create(response=resp1, field=field, defaults={'value': val})

    # Response 2: Rahul registered for Next.js Workshop
    resp2, _ = Response.objects.get_or_create(
        form=f2,
        user=rahul,
        defaults={'form_version': 1}
    )
    for field in f2.fields.all():
        val = 'Rahul Sharma' if 'Name' in field.label else (
            'rahul.sharma@srkr.ac.in' if 'Email' in field.label else 'Intermediate'
        )
        Answer.objects.get_or_create(response=resp2, field=field, defaults={'value': val})

    # Response 3: Rahul registered for CodeQuest Daily
    resp3, _ = Response.objects.get_or_create(
        form=f3,
        user=rahul,
        defaults={'form_version': 1}
    )
    for field in f3.fields.all():
        val = 'Rahul Sharma' if 'Name' in field.label else 'Python'
        Answer.objects.get_or_create(response=resp3, field=field, defaults={'value': val})

    # Response 4: Member Kiran Kumar registered for Next.js workshop
    kiran = users_map['member@srkr.ac.in']
    resp4, _ = Response.objects.get_or_create(
        form=f2,
        user=kiran,
        defaults={'form_version': 1}
    )
    for field in f2.fields.all():
        val = 'Kiran Kumar' if 'Name' in field.label else (
            'member@srkr.ac.in' if 'Email' in field.label else 'Beginner'
        )
        Answer.objects.get_or_create(response=resp4, field=field, defaults={'value': val})

    print(" [Responses] Seeded live form responses & answer data.")

    # 5. Seed Events & Feature Flags
    Event.objects.get_or_create(
        slug='iconcoders-2025-flagship',
        defaults={
            'title': 'IconCoders Flagship Hackathon 2025',
            'description': 'Annual flagship 48-hour build sprint across AI/ML, Web3, and Open Tech.',
            'category': 'Hackathon',
            'venue': 'Auditorium & Innovation Labs',
            'capacity': 250,
            'start_time': timezone.now() + timedelta(days=15),
            'end_time': timezone.now() + timedelta(days=17),
            'registration_form': f1,
        }
    )

    Event.objects.get_or_create(
        slug='nextjs-15-masterclass-workshop',
        defaults={
            'title': 'Hands-on Web Dev & Next.js Workshop',
            'description': 'Deep-dive into App Router, React 19 Server Actions, and Tailwind CSS.',
            'category': 'Workshop',
            'venue': 'CSE Seminar Hall',
            'capacity': 120,
            'start_time': timezone.now() + timedelta(days=5),
            'end_time': timezone.now() + timedelta(days=6),
            'registration_form': f2,
        }
    )

    # Feature Flags
    flags = [
        ('module_hackathons', 'Hackathons Engine Module', True),
        ('module_iconcoders', 'IconCoders Premier Arena', True),
        ('module_codequest', 'Codequest Daily Algorithm Challenge', True),
        ('module_blogs', 'Student Blogs & Tech Articles', True),
        ('module_career', 'Career Opportunities & Job Board', True),
    ]
    for key, name, enabled in flags:
        FeatureFlag.objects.get_or_create(
            key=key,
            defaults={'name': name, 'is_enabled': enabled, 'description': f'Controls visibility of {name}'}
        )

    # Initial Audit Logs
    AuditLog.objects.get_or_create(
        action="System Master Seed Complete",
        target_model="Platform",
        target_id="1",
        defaults={
            'actor': admin,
            'details': {"status": "Complete", "modules": ["Accounts", "Forms", "Events", "CodeQuest", "Audit"]}
        }
    )

    print("=" * 70)
    print("Done! Master database is fully seeded with live users, forms, and responses.")

if __name__ == '__main__':
    seed_database()
