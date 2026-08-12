from django.core.management.base import BaseCommand
from django.utils import timezone
from django.contrib.auth import get_user_model
from datetime import timedelta

from apps.accounts.models import UserRole
from apps.feature_flags.models import FeatureFlag
from apps.forms.models import Form, FormField, FormStatus, FieldType
from apps.events.models import Event
from apps.hackathons.models import Hackathon, Team, Submission
from apps.codequest.models import Problem, Difficulty, UserStreak
from apps.career.models import JobListing, JobType
from apps.blogs.models import BlogPost
from apps.audit.models import AuditLog

User = get_user_model()

class Command(BaseCommand):
    help = "Ingest mock dataset across all SRKR Coding Club backend modules"

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS("Starting database mock data ingestion..."))

        # 1. Create Core Users & RBAC
        admin_user, _ = User.objects.get_or_create(
            email="admin@srkr.ac.in",
            defaults={
                "username": "admin",
                "first_name": "Admin",
                "last_name": "User",
                "role": UserRole.ADMIN,
                "roll_number": "ADMIN001",
                "branch": "CSE",
                "year": 4,
                "is_staff": True,
                "is_superuser": True,
            }
        )
        admin_user.set_password("admin123")
        admin_user.save()

        lead_user, _ = User.objects.get_or_create(
            email="clublead@srkr.ac.in",
            defaults={
                "username": "clublead",
                "first_name": "Rahul",
                "last_name": "Sharma",
                "role": UserRole.CLUB_LEAD,
                "roll_number": "21B91A0501",
                "branch": "CSE",
                "year": 3,
                "is_staff": True,
            }
        )
        lead_user.set_password("lead123")
        lead_user.save()

        judge_user, _ = User.objects.get_or_create(
            email="judge@srkr.ac.in",
            defaults={
                "username": "judge",
                "first_name": "Karthik",
                "last_name": "Raju",
                "role": UserRole.JUDGE,
                "roll_number": "21B91A0588",
                "branch": "CSE",
                "year": 3,
            }
        )
        judge_user.set_password("judge123")
        judge_user.save()

        member_user, _ = User.objects.get_or_create(
            email="member@srkr.ac.in",
            defaults={
                "username": "member",
                "first_name": "Priya",
                "last_name": "Rao",
                "role": UserRole.MEMBER,
                "roll_number": "23B91A0412",
                "branch": "ECE",
                "year": 1,
            }
        )
        member_user.set_password("member123")
        member_user.save()

        self.stdout.write(self.style.SUCCESS("[OK] User accounts created."))

        # 2. Create Feature Flags
        flags_data = [
            ("module_hackathons", "Hackathons Platform", "Enables registration & submission engine for hackathons.", True),
            ("module_codequest", "Codequest Daily Engine", "Enables daily coding problem streak challenges.", True),
            ("module_events", "Events Showcase", "Public workshops and seminar schedules.", True),
            ("module_forms", "Forms Center Engine", "Dynamic form builder and registration collector.", True),
            ("module_blogs", "Blogs & Write-ups Hub", "Community technical blog publishing hub.", True),
            ("module_career", "Career Placement Drive", "Job listings, internships & candidate drive engine.", True),
        ]

        for key, name, desc, is_enabled in flags_data:
            FeatureFlag.objects.get_or_create(
                key=key,
                defaults={"name": name, "description": desc, "is_enabled": is_enabled}
            )

        self.stdout.write(self.style.SUCCESS("[OK] Feature flags ingested."))

        # 3. Create Dynamic Forms & Fields
        now = timezone.now()
        form_1, _ = Form.objects.get_or_create(
            slug="iconcoders-hackathon-2025",
            defaults={
                "title": "IconCoders Flagship Hackathon 2025 Registration",
                "description": "Official registration form for SRKR Coding Club annual flagship hackathon.",
                "status": FormStatus.PUBLISHED,
                "open_at": now,
                "close_at": now + timedelta(days=30),
            }
        )

        fields_f1 = [
            ("Team Leader Name", FieldType.TEXT, "e.g. Ramesh Varma", True, [], 1),
            ("Email Address", FieldType.EMAIL, "student@srkr.ac.in", True, [], 2),
            ("Roll Number & Branch", FieldType.TEXT, "21B91A0501 CSE", True, [], 3),
            ("Selected Track", FieldType.DROPDOWN, "Select track", True, ["AI/ML", "Web Dev", "Mobile App", "Blockchain", "Open Innovation"], 4),
        ]

        for label, ftype, placeholder, is_req, opts, order in fields_f1:
            FormField.objects.get_or_create(
                form=form_1,
                label=label,
                defaults={
                    "type": ftype,
                    "placeholder": placeholder,
                    "is_required": is_req,
                    "options": opts,
                    "order": order,
                }
            )

        form_2, _ = Form.objects.get_or_create(
            slug="web-dev-workshop-rsvp",
            defaults={
                "title": "Web Development Workshop RSVP & Tool Kit",
                "description": "Reserve your physical seat for hands-on React & Next.js workshop.",
                "status": FormStatus.CLOSED,
                "open_at": now - timedelta(days=10),
                "close_at": now - timedelta(days=1),
            }
        )

        FormField.objects.get_or_create(
            form=form_2,
            label="Full Name",
            defaults={"type": FieldType.TEXT, "is_required": True, "order": 1}
        )
        FormField.objects.get_or_create(
            form=form_2,
            label="College Email",
            defaults={"type": FieldType.EMAIL, "is_required": True, "order": 2}
        )
        FormField.objects.get_or_create(
            form=form_2,
            label="Year of Study",
            defaults={"type": FieldType.RADIO, "options": ["1st Year", "2nd Year", "3rd Year", "4th Year"], "is_required": True, "order": 3}
        )

        self.stdout.write(self.style.SUCCESS("[OK] Forms & dynamic fields ingested."))

        # 4. Create Events
        Event.objects.get_or_create(
            slug="full-stack-react-nextjs-workshop",
            defaults={
                "title": "Full Stack React & Next.js 15 Hands-on Workshop",
                "description": "Master modern frontend development, App Router server components, and Tailwind CSS glassmorphism styling.",
                "category": "Hands-on Workshop",
                "venue": "SRKR Central Seminar Hall",
                "capacity": 150,
                "poster_image": "https://images.unsplash.com/photo-1526374965328-7f61d4dc18c5?auto=format&fit=crop&w=800&q=80",
                "start_time": now + timedelta(days=5),
                "end_time": now + timedelta(days=5, hours=6),
                "registration_form": form_2,
            }
        )

        Event.objects.get_or_create(
            slug="ai-generative-llm-seminar",
            defaults={
                "title": "AI & Generative LLMs Model Fine-Tuning Seminar",
                "description": "Explore PyTorch, LoRA fine-tuning, and open-source model deployment strategies.",
                "category": "Tech Seminar",
                "venue": "CSE Department Lab 3",
                "capacity": 100,
                "poster_image": "https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?auto=format&fit=crop&w=800&q=80",
                "start_time": now + timedelta(days=12),
                "end_time": now + timedelta(days=12, hours=3),
            }
        )

        self.stdout.write(self.style.SUCCESS("[OK] Events ingested."))

        # 5. Create Hackathons & Teams
        h1, _ = Hackathon.objects.get_or_create(
            slug="iconcoders-hackathon-2025",
            defaults={
                "title": "IconCoders Flagship Hackathon 2025",
                "is_flagship": True,
                "theme": "AI for Social Good & Web3 Innovations",
                "description": "SRKR Coding Club annual flagship 36-hour hackathon.",
                "prize_pool": "₹1,00,000 INR",
                "banner_image": "https://images.unsplash.com/photo-1555066931-4365d14bab8c?auto=format&fit=crop&w=1200&q=80",
                "start_date": now + timedelta(days=20),
                "end_date": now + timedelta(days=22),
                "registration_form": form_1,
            }
        )

        t1, _ = Team.objects.get_or_create(
            name="Alpha Coders",
            hackathon=h1,
            defaults={"leader": lead_user}
        )

        Submission.objects.get_or_create(
            team=t1,
            defaults={
                "project_title": "Smart Campus Assistant",
                "description": "AI-powered campus navigation and event tracking solution.",
                "repo_url": "https://github.com/srkrcc/smart-campus",
                "score": 92.5,
            }
        )

        self.stdout.write(self.style.SUCCESS("[OK] Hackathons & submissions ingested."))

        # 6. Create Codequest Problems & Streaks
        Problem.objects.get_or_create(
            slug="subarray-max-bitwise-or-value",
            defaults={
                "title": "Problem #142: Subarray Maximum Bitwise OR Value",
                "difficulty": Difficulty.MEDIUM,
                "statement": "Given an integer array nums, find the maximum possible bitwise OR value of any non-empty contiguous subarray.",
                "constraints": "1 <= nums.length <= 10^5, 0 <= nums[i] <= 10^9",
                "scheduled_date": now.date(),
                "tags": ["Bit Manipulation", "Arrays"],
            }
        )

        Problem.objects.get_or_create(
            slug="valid-palindrome-substring-replacement",
            defaults={
                "title": "Problem #140: Valid Palindrome Substring Replacement",
                "difficulty": Difficulty.EASY,
                "statement": "Given a string s, return true if you can transform s into a palindrome by changing at most k characters.",
                "constraints": "1 <= s.length <= 1000",
                "scheduled_date": now.date() - timedelta(days=1),
                "tags": ["Strings", "Two Pointers"],
            }
        )

        UserStreak.objects.get_or_create(
            user=member_user,
            defaults={"current_streak": 14, "max_streak": 30, "last_solved_date": now.date()}
        )

        self.stdout.write(self.style.SUCCESS("[OK] Codequest problems & streaks ingested."))

        # 7. Create Career Placement Listings
        JobListing.objects.get_or_create(
            slug="full-stack-software-engineer-intern",
            defaults={
                "title": "Full Stack Software Engineer Intern",
                "company_name": "Tech Corp Solutions",
                "job_type": JobType.INTERNSHIP,
                "location": "Hyderabad / Hybrid",
                "salary_range": "₹40,000 / month",
                "description": "Looking for CSE/IT students proficient in React and Django.",
                "deadline": now + timedelta(days=40),
            }
        )

        self.stdout.write(self.style.SUCCESS("[OK] Career placement listings ingested."))

        # 8. Create Technical Blogs
        BlogPost.objects.get_or_create(
            slug="building-scalable-microservices-nextjs-django",
            defaults={
                "title": "Building Scalable Microservices with Next.js 15 & Django REST Framework",
                "author": lead_user,
                "excerpt": "Learn how to architect high-performance full-stack web applications by leveraging Next.js 15 Server Components alongside Django SimpleJWT backend authentication.",
                "content": "Full step-by-step tutorial on building production-grade web systems...",
                "cover_image": "https://images.unsplash.com/photo-1555066931-4365d14bab8c?auto=format&fit=crop&w=1200&q=80",
                "tags": ["Next.js 15", "Django", "Architecture"],
                "is_published": True,
                "published_at": now,
            }
        )

        self.stdout.write(self.style.SUCCESS("[OK] Technical blogs ingested."))

        # 9. Create Audit Logs
        AuditLog.objects.create(
            actor=admin_user,
            action="Executed Mock Data Seed Command",
            target_model="System",
            target_id="SEED_001",
            details={"status": "SUCCESS", "records_ingested": "All 9 Modules"}
        )

        self.stdout.write(self.style.SUCCESS("\n[SUCCESS] Database mock data ingestion complete across all 9 modules!"))
