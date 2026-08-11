from django.contrib import admin
from .models import Form, FormField, Response, Answer

class FormFieldInline(admin.TabularInline):
    model = FormField
    extra = 1

@admin.register(Form)
class FormAdmin(admin.ModelAdmin):
    list_display = ['title', 'slug', 'status', 'open_at', 'close_at']
    list_filter = ['status']
    inlines = [FormFieldInline]

class AnswerInline(admin.TabularInline):
    model = Answer
    extra = 0
    readonly_fields = ['field', 'value']

@admin.register(Response)
class ResponseAdmin(admin.ModelAdmin):
    list_display = ['id', 'form', 'user', 'submitted_at']
    inlines = [AnswerInline]
