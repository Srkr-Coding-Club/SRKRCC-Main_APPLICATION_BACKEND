from django.contrib import admin
from .models import Problem, Submission, UserStreak

@admin.register(Problem)
class ProblemAdmin(admin.ModelAdmin):
    list_display = ['title', 'scheduled_date', 'difficulty']
    list_filter = ['difficulty', 'scheduled_date']

@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = ['user', 'problem', 'is_correct', 'created_at']

@admin.register(UserStreak)
class UserStreakAdmin(admin.ModelAdmin):
    list_display = ['user', 'current_streak', 'max_streak', 'last_solved_date']
