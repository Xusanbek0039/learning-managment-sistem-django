from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.utils.html import format_html
from .models import (
    CustomUser, Profession, CourseEnrollment, Lesson, VideoLesson, VideoProgress,
    Homework, HomeworkSubmission, Test, TestQuestion, TestAnswer, TestResult,
    Certificate, CoinTransaction, Message, PaymentStatus
)


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    list_display = ['username', 'full_name', 'phone', 'role', 'coins', 'is_blocked', 'is_online_display', 'date_joined']
    list_filter = ['role', 'is_blocked', 'profession', 'date_joined']
    search_fields = ['username', 'first_name', 'last_name', 'phone']
    ordering = ['-date_joined']
    list_editable = ['is_blocked']
    list_per_page = 25
    
    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        ('Shaxsiy ma\'lumotlar', {'fields': ('first_name', 'last_name', 'phone', 'email', 'photo', 'bio')}),
        ('Rol va yo\'nalish', {'fields': ('role', 'profession', 'coins')}),
        ('Status', {'fields': ('is_blocked', 'is_active', 'is_staff', 'is_superuser')}),
        ('Vaqtlar', {'fields': ('last_activity', 'last_login', 'date_joined')}),
        ('Ruxsatlar', {'fields': ('groups', 'user_permissions')}),
    )
    
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('username', 'first_name', 'last_name', 'phone', 'role', 'password1', 'password2'),
        }),
    )
    
    readonly_fields = ['last_login', 'date_joined', 'last_activity']
    
    def full_name(self, obj):
        return obj.full_name
    full_name.short_description = 'Ism Familiya'
    
    def is_online_display(self, obj):
        if obj.is_online:
            return format_html('<span style="color: #28a745;">●</span> Online')
        return format_html('<span style="color: #6c757d;">●</span> Offline')
    is_online_display.short_description = 'Status'


@admin.register(Profession)
class ProfessionAdmin(admin.ModelAdmin):
    list_display = ['name', 'student_count', 'teacher_count', 'lesson_count', 'created_at']
    search_fields = ['name', 'description']
    list_per_page = 20
    
    def student_count(self, obj):
        count = obj.enrollments.filter(user__role='student').count()
        return format_html('<span class="badge bg-primary">{}</span>', count)
    student_count.short_description = "O'quvchilar"
    
    def teacher_count(self, obj):
        count = obj.students.filter(role='teacher').count()
        return format_html('<span class="badge bg-success">{}</span>', count)
    teacher_count.short_description = "O'qituvchilar"
    
    def lesson_count(self, obj):
        return obj.lessons.count()
    lesson_count.short_description = 'Darslar'


@admin.register(CourseEnrollment)
class CourseEnrollmentAdmin(admin.ModelAdmin):
    list_display = ['user', 'profession', 'enrolled_at']
    list_filter = ['profession', 'enrolled_at']
    search_fields = ['user__username', 'user__first_name', 'profession__name']
    autocomplete_fields = ['user', 'profession']
    date_hierarchy = 'enrolled_at'


@admin.register(Lesson)
class LessonAdmin(admin.ModelAdmin):
    list_display = ['title', 'profession', 'lesson_type', 'created_by', 'created_at']
    list_filter = ['lesson_type', 'profession', 'created_at']
    search_fields = ['title', 'profession__name']
    autocomplete_fields = ['profession', 'created_by']


@admin.register(VideoLesson)
class VideoLessonAdmin(admin.ModelAdmin):
    list_display = ('lesson', 'duration', 'youtube_url', 'embed_preview')
    search_fields = ('lesson__title',)
    readonly_fields = ('embed_preview',)

    fieldsets = (
        (None, {
            'fields': ('lesson', 'youtube_url', 'duration')
        }),
        ('Video Preview', {
            'fields': ('embed_preview',),
        }),
    )


@admin.register(Homework)
class HomeworkAdmin(admin.ModelAdmin):
    list_display = ['lesson', 'due_date']
    search_fields = ['lesson__title']


@admin.register(HomeworkSubmission)
class HomeworkSubmissionAdmin(admin.ModelAdmin):
    list_display = ['student', 'homework', 'status', 'grade', 'submitted_at']
    list_filter = ['status', 'submitted_at']
    search_fields = ['student__username', 'homework__lesson__title']
    list_editable = ['status', 'grade']


@admin.register(Test)
class TestAdmin(admin.ModelAdmin):
    list_display = ['lesson', 'time_limit', 'passing_score', 'question_count']
    search_fields = ['lesson__title']
    
    def question_count(self, obj):
        return obj.questions.count()
    question_count.short_description = 'Savollar'


class TestAnswerInline(admin.TabularInline):
    model = TestAnswer
    extra = 3
    max_num = 4


@admin.register(TestQuestion)
class TestQuestionAdmin(admin.ModelAdmin):
    list_display = ['question_text_short', 'test', 'order']
    list_filter = ['test__lesson__profession']
    search_fields = ['question_text', 'test__lesson__title']
    inlines = [TestAnswerInline]
    
    def question_text_short(self, obj):
        return obj.question_text[:50] + '...' if len(obj.question_text) > 50 else obj.question_text
    question_text_short.short_description = 'Savol'


@admin.register(TestResult)
class TestResultAdmin(admin.ModelAdmin):
    list_display = ['student', 'test', 'score', 'passed', 'completed_at']
    list_filter = ['passed', 'completed_at', 'test__lesson__profession']
    search_fields = ['student__username', 'test__lesson__title']
    date_hierarchy = 'completed_at'
    
    def passed(self, obj):
        if obj.passed:
            return format_html('<span style="color: #28a745;">✓ O\'tdi</span>')
        return format_html('<span style="color: #dc3545;">✗ O\'tmadi</span>')
    passed.short_description = 'Natija'


@admin.register(Certificate)
class CertificateAdmin(admin.ModelAdmin):
    list_display = ['student', 'profession', 'issued_by', 'issued_at']
    list_filter = ['profession', 'issued_at']
    search_fields = ['student__username', 'profession__name']
    autocomplete_fields = ['student', 'profession', 'issued_by']
    date_hierarchy = 'issued_at'


@admin.register(CoinTransaction)
class CoinTransactionAdmin(admin.ModelAdmin):
    list_display = ['user', 'amount', 'reason', 'created_at']
    list_filter = ['created_at']
    search_fields = ['user__username', 'reason']
    date_hierarchy = 'created_at'
    readonly_fields = ['user', 'amount', 'reason', 'created_at']


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ['title', 'message_type', 'recipient', 'sender', 'is_read', 'created_at']
    list_filter = ['message_type', 'is_read', 'created_at']
    search_fields = ['title', 'content', 'recipient__username']
    date_hierarchy = 'created_at'


@admin.register(PaymentStatus)
class PaymentStatusAdmin(admin.ModelAdmin):
    list_display = ['user', 'is_paid', 'last_payment_date', 'auto_blocked']
    list_filter = ['is_paid', 'auto_blocked']
    search_fields = ['user__username', 'user__first_name']
    list_editable = ['is_paid']


# Admin site customization
admin.site.site_header = "LMS Admin Panel"
admin.site.site_title = "LMS"
admin.site.index_title = "Boshqaruv paneli"
