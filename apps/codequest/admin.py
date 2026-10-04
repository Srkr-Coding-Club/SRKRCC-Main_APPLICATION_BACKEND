from django.contrib import admin
from django.urls import path
from .models import Problem, Submission, UserStreak


@admin.register(Problem)
class ProblemAdmin(admin.ModelAdmin):
    list_display = ['title', 'scheduled_date', 'difficulty']
    list_filter = ['difficulty', 'scheduled_date']

    actions = ['batch_schedule_problems']

    @admin.action(description='Batch schedule 5 Codequest problems with dates')
    def batch_schedule_problems(self, request, queryset):
        """
        Custom admin action to batch schedule problems.
        Opens a simple inline form for entering 5 problem titles and 5 dates.
        """
        from django.contrib import messages
        from django.shortcuts import render, redirect

        if request.method == 'POST':
            titles = request.POST.getlist('titles[]')
            dates = request.POST.getlist('dates[]')
            difficulties = request.POST.getlist('difficulties[]') or ['EASY'] * len(titles)

            if len(titles) < 1 or len(titles) > 5:
                self.message_user(request, 'Please enter between 1 and 5 problem titles.', messages.ERROR)
                return

            if len(titles) != len(dates):
                self.message_user(request, 'Number of titles must match number of dates.', messages.ERROR)
                return

            created = 0
            errors = []

            for i, title in enumerate(titles):
                try:
                    from datetime import datetime
                    date_obj = datetime.strptime(dates[i], '%Y-%m-%d').date()

                    base_slug = __import__('django.utils.text').slugify(title) or 'codequest-problem'
                    slug = base_slug
                    suffix = 2
                    while Problem.objects.filter(slug=slug).exists():
                        slug = f'{base_slug}-{suffix}'
                        suffix += 1

                    Problem.objects.create(
                        title=title,
                        slug=slug,
                        difficulty=difficulties[i] if i < len(difficulties) else 'EASY',
                        statement=f'Codequest problem: {title}',
                        constraints='',
                        sample_input='',
                        sample_output='',
                        tags=[],
                        external_url='',
                        external_platform='',
                        scheduled_date=date_obj,
                    )
                    created += 1
                except Exception as e:
                    errors.append(f'Error with "{title}": {str(e)}')

            if errors:
                self.message_user(request, f'Created {created} problems. Errors: {", ".join(errors)}', messages.WARNING)
            else:
                self.message_user(request, f'Successfully scheduled {created} problem(s)', messages.SUCCESS)
            return

        # Show the batch scheduling form
        from django.template.response import TemplateResponse
        today = request.user.timezone.localdate() if hasattr(request, 'user') and request.user else None
        problems = Problem.objects.filter(scheduled_date__lte=today).order_by('-scheduled_date')[:5] if today else []

        context = {
            'title': 'Batch Schedule Codequest Problems',
            'opts': self.model._meta,
            'app_label': self.model._meta.app_label,
            'existing_problems': problems,
        }
        return TemplateResponse(request, 'admin/codequest/batch_schedule.html', context)


@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = ['user', 'problem', 'is_correct', 'created_at']

@admin.register(UserStreak)
class UserStreakAdmin(admin.ModelAdmin):
    list_display = ['user', 'current_streak', 'max_streak', 'last_solved_date']
