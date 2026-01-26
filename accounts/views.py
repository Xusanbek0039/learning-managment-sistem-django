from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.db.models import Count, Q, Sum, Avg
from django.db import models
from django.utils import timezone
from datetime import timedelta
from .forms import (
    RegisterForm, LoginForm, ProfessionForm, UserProfileForm, 
    AdminUserEditForm, ChangePasswordForm, VideoLessonForm, HomeworkForm,
    HomeworkSubmissionForm, HomeworkGradeForm, TestForm, TestQuestionForm, CertificateForm,
    SectionForm
)
from .models import (
    Profession, Section, CustomUser, CourseEnrollment, Lesson, VideoLesson, VideoProgress,
    Homework, HomeworkSubmission, Test, TestQuestion, TestAnswer, TestResult,
    TestUserAnswer, Certificate, Message, PaymentStatus, HelpRequest, Discount,
    UserDevice, UserSession, HTMLDeploy
)
from coin.models import ActivityLog, CoinTransaction

from django.core.management import call_command
from django.core.cache import cache


def send_course_notification(profession, title, content, sender=None):
    """Kursga yozilgan barcha o'quvchilarga xabar yuborish"""
    enrollments = CourseEnrollment.objects.filter(profession=profession)
    for enrollment in enrollments:
        Message.objects.create(
            sender=sender,
            recipient=enrollment.user,
            title=title,
            content=content,
            message_type='system'
        )


def register_view(request):
    if request.user.is_authenticated:
        return redirect('home')
    
    if request.method == 'POST':
        form = RegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, "Ro'yxatdan muvaffaqiyatli o'tdingiz!")
            return redirect('home')
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{error}")
    else:
        form = RegisterForm()
    
    return render(request, 'accounts/register.html', {'form': form})



def login_view(request):
    if request.user.is_authenticated:
        return redirect('home')

    if request.method == 'POST':
        form = LoginForm(request.POST)

        if form.is_valid():
            username = form.cleaned_data.get('username')
            password = form.cleaned_data.get('password')

            user = authenticate(request, username=username, password=password)

            # ❌ Username yoki parol xato
            if user is None:
                messages.error(request, "Username yoki parol noto‘g‘ri ❌")
                return render(request, 'accounts/login.html', {'form': form})

            # ⛔ Bloklangan user
            if hasattr(user, "is_blocked") and user.is_blocked:
                messages.error(request, "Sizning akkauntingiz bloklangan 🚫")
                return render(request, 'accounts/login.html', {'form': form})

            # ✅ Login successful
            login(request, user)

            user.last_activity = timezone.now()
            user.save(update_fields=["last_activity"])

            ActivityLog.objects.create(
                user=user,
                action_type='login',
                description="Tizimga kirdi",
                ip_address=request.META.get('REMOTE_ADDR')
            )

            messages.success(request, "Tizimga muvaffaqiyatli kirdingiz! 🎉")
            return redirect('home')

        else:
            # ❗ Form validation xatolari (bo‘sh qoldirilgan va h.k.)
            messages.error(request, "Iltimos, barcha maydonlarni to‘g‘ri to‘ldiring ⚠️")

    else:
        form = LoginForm()

    return render(request, 'accounts/login.html', {'form': form})


def logout_view(request):
    if request.user.is_authenticated:
        ActivityLog.objects.create(
            user=request.user,
            action_type='logout',
            description="Tizimdan chiqdi",
            ip_address=request.META.get('REMOTE_ADDR')
        )

    logout(request)
    messages.success(request, "Tizimdan muvaffaqiyatli chiqdingiz 👋")
    return redirect('login')


@login_required
def home_view(request):
    professions = Profession.objects.all()[:6]
    
    # Statistika
    total_students = CustomUser.objects.filter(role='student').count()
    total_courses = Profession.objects.count()
    total_lessons = Lesson.objects.count()
    
    # O'quvchi uchun uning kurslari
    my_enrollments = []
    if request.user.is_student:
        my_enrollments = request.user.enrollments.all()[:4]
    
    context = {
        'professions': professions,
        'total_students': total_students,
        'total_courses': total_courses,
        'total_lessons': total_lessons,
        'my_enrollments': my_enrollments,
    }
    return render(request, 'accounts/home.html', context)


@login_required
def professions_view(request):
    professions = Profession.objects.all()
    enrolled_ids = []
    if request.user.is_student:
        enrolled_ids = request.user.enrollments.values_list('profession_id', flat=True)
    return render(request, 'accounts/professions.html', {'professions': professions, 'enrolled_ids': enrolled_ids})


@login_required
def profession_detail(request, pk):
    profession = get_object_or_404(Profession, pk=pk)
    is_enrolled = False
    if request.user.is_student:
        is_enrolled = CourseEnrollment.objects.filter(user=request.user, profession=profession).exists()
    
    sections = []
    lessons_without_section = []
    lesson_progress = {}
    
    if is_enrolled or request.user.is_teacher or request.user.is_admin:
        sections = profession.sections.filter(is_active=True).prefetch_related('lessons')
        lessons_without_section = profession.lessons.filter(section__isnull=True)
        
        # Get user progress for lessons
        if request.user.is_authenticated:
            # Video progress
            watched_videos = VideoProgress.objects.filter(
                user=request.user, 
                watched=True,
                video__lesson__profession=profession
            ).values_list('video__lesson_id', flat=True)
            
            # Test results
            completed_tests = TestResult.objects.filter(
                student=request.user,
                test__lesson__profession=profession
            ).values_list('test__lesson_id', flat=True)
            
            # Homework submissions
            submitted_homeworks = HomeworkSubmission.objects.filter(
                student=request.user,
                homework__lesson__profession=profession
            ).values('homework__lesson_id', 'grade')
            
            for vid in watched_videos:
                lesson_progress[vid] = {'completed': True, 'type': 'video'}
            
            for test_id in completed_tests:
                lesson_progress[test_id] = {'completed': True, 'type': 'test'}
            
            for hw in submitted_homeworks:
                lesson_progress[hw['homework__lesson_id']] = {
                    'completed': True, 
                    'type': 'homework',
                    'graded': hw['grade'] is not None
                }
    
    # Kursdoshlar - shu kursga yozilgan o'quvchilar
    classmates = []
    if is_enrolled or request.user.is_teacher or request.user.is_admin:
        classmates = CustomUser.objects.filter(
            enrollments__profession=profession,
            role='student'
        ).exclude(pk=request.user.pk).order_by('-last_activity', '-coins')[:20]
    
    return render(request, 'accounts/profession_detail.html', {
        'profession': profession,
        'is_enrolled': is_enrolled,
        'sections': sections,
        'lessons_without_section': lessons_without_section,
        'lesson_progress': lesson_progress,
        'classmates': classmates,
    })


@login_required
def enroll_course(request, pk):
    if not request.user.is_student:
        messages.error(request, "Faqat o'quvchilar kursga yozilishi mumkin!")
        return redirect('professions')
    
    profession = get_object_or_404(Profession, pk=pk)
    
    if request.method == 'POST':
        enrollment, created = CourseEnrollment.objects.get_or_create(user=request.user, profession=profession)
        if created:
            messages.success(request, f"Siz '{profession.name}' kursiga muvaffaqiyatli yozildingiz!")
        return redirect('profession_detail', pk=pk)
    
    return render(request, 'accounts/enroll_confirm.html', {'profession': profession})


# Lesson views
@login_required
def lesson_view(request, pk):
    lesson = get_object_or_404(Lesson, pk=pk)
    profession = lesson.profession
    
    if request.user.is_student:
        if not CourseEnrollment.objects.filter(user=request.user, profession=profession).exists():
            messages.error(request, "Avval kursga yoziling!")
            return redirect('profession_detail', pk=profession.pk)
    
    context = {'lesson': lesson, 'profession': profession}
    
    if lesson.lesson_type == 'video':
        video = lesson.video
        progress, _ = VideoProgress.objects.get_or_create(user=request.user, video=video)
        context['video'] = video
        context['progress'] = progress
        return render(request, 'accounts/lessons/video_lesson.html', context)
    
    elif lesson.lesson_type == 'homework':
        homework = lesson.homework
        submission = HomeworkSubmission.objects.filter(homework=homework, student=request.user).first()
        context['homework'] = homework
        context['submission'] = submission
        context['form'] = HomeworkSubmissionForm()
        return render(request, 'accounts/lessons/homework_lesson.html', context)
    
    elif lesson.lesson_type == 'test':
        test = lesson.test
        result = TestResult.objects.filter(test=test, student=request.user).order_by('-completed_at').first()
        context['test'] = test
        context['result'] = result
        return render(request, 'accounts/lessons/test_lesson.html', context)
    
    return redirect('profession_detail', pk=profession.pk)


@login_required
def mark_video_watched(request, pk):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST only'})
    
    video = get_object_or_404(VideoLesson, pk=pk)
    progress, _ = VideoProgress.objects.get_or_create(user=request.user, video=video)
    
    if not progress.watched:
        progress.watched = True
        progress.watched_at = timezone.now()
        progress.save()
        
        if not progress.coin_awarded:
            request.user.add_coins(1, f"Video darslik ko'rildi: {video.lesson.title}")
            progress.coin_awarded = True
            progress.save()
    
    return JsonResponse({'success': True, 'coins': request.user.coins})


@login_required
def submit_homework(request, pk):
    homework = get_object_or_404(Homework, pk=pk)
    
    if request.method == 'POST':
        form = HomeworkSubmissionForm(request.POST, request.FILES)
        if form.is_valid():
            submission = form.save(commit=False)
            submission.homework = homework
            submission.student = request.user
            submission.save()
            messages.success(request, "Vazifa muvaffaqiyatli yuborildi!")
    
    return redirect('lesson_view', pk=homework.lesson.pk)


@login_required
def start_test(request, pk):
    test = get_object_or_404(Test, pk=pk)
    
    if request.user.is_student:
        profession = test.lesson.profession
        if not CourseEnrollment.objects.filter(user=request.user, profession=profession).exists():
            messages.error(request, "Avval kursga yoziling!")
            return redirect('profession_detail', pk=profession.pk)
        
        # Check if already taken and retry not allowed
        existing_result = TestResult.objects.filter(test=test, student=request.user).first()
        if existing_result and not test.allow_retry:
            messages.info(request, "Siz bu testni allaqachon topshirgansiz.")
            return redirect('test_result', pk=existing_result.pk)
    
    questions = test.questions.all()
    request.session[f'test_{test.pk}_start'] = timezone.now().isoformat()
    
    return render(request, 'accounts/lessons/test_take.html', {
        'test': test,
        'questions': questions,
    })


@login_required
def submit_test(request, pk):
    test = get_object_or_404(Test, pk=pk)
    
    if request.method != 'POST':
        return redirect('start_test', pk=pk)
    
    start_time = request.session.get(f'test_{test.pk}_start')
    started_at = timezone.now()
    if start_time:
        from datetime import datetime
        started_at = datetime.fromisoformat(start_time)
    
    questions = test.questions.all()
    correct = 0
    total = questions.count()
    
    result = TestResult.objects.create(
        test=test,
        student=request.user,
        score=0,
        total_questions=total,
        correct_answers=0,
        started_at=started_at,
        passed=False
    )
    
    for question in questions:
        answer_id = request.POST.get(f'question_{question.pk}')
        selected_answer = None
        is_correct = False
        
        if answer_id:
            try:
                selected_answer = TestAnswer.objects.get(pk=answer_id)
                is_correct = selected_answer.is_correct
                if is_correct:
                    correct += 1
            except TestAnswer.DoesNotExist:
                pass
        
        TestUserAnswer.objects.create(
            result=result,
            question=question,
            selected_answer=selected_answer,
            is_correct=is_correct
        )
    
    score = int((correct / total) * 100) if total > 0 else 0
    result.score = score
    result.correct_answers = correct
    result.passed = score >= test.passing_score
    result.save()
    
    # Har bir to'g'ri javob uchun 1 coin
    if correct > 0 and not result.coin_awarded:
        request.user.add_coins(correct, f"Test: {correct} ta to'g'ri javob - {test.lesson.title}")
        result.coin_awarded = True
        result.save()
        
        # Activity log
        ActivityLog.objects.create(
            user=request.user,
            action_type='Test topshirdi',
            description=f"Test topshirdi: {test.lesson.title} - {correct}/{total} to'g'ri, {correct} coin oldi"
        )
        
        messages.success(request, f"Tabriklaymiz! {correct} ta to'g'ri javob uchun {correct} coin oldingiz!")
    
    return redirect('test_result', pk=result.pk)


@login_required
def test_result(request, pk):
    result = get_object_or_404(TestResult, pk=pk)
    user_answers = result.user_answers.all().select_related('question', 'selected_answer')
    
    return render(request, 'accounts/lessons/test_result.html', {
        'result': result,
        'user_answers': user_answers,
    })


# Profile views
@login_required
def profile_view(request):
    certificates = Certificate.objects.filter(student=request.user).select_related('profession')
    enrollments = request.user.enrollments.all().select_related('profession')
    
    # Tugallangan kurslar (sertifikat berilgan)
    completed_profession_ids = certificates.values_list('profession_id', flat=True)
    completed_courses = []
    active_courses = []
    
    for enrollment in enrollments:
        if enrollment.profession_id in completed_profession_ids:
            cert = certificates.filter(profession_id=enrollment.profession_id).first()
            completed_courses.append({
                'enrollment': enrollment,
                'certificate': cert,
            })
        else:
            active_courses.append(enrollment)
    
    return render(request, 'accounts/profile.html', {
        'certificates': certificates,
        'enrollments': enrollments,
        'completed_courses': completed_courses,
        'active_courses': active_courses,
    })


@login_required
def classmates_view(request, pk):
    """Kursdoshlar ro'yxati"""
    profession = get_object_or_404(Profession, pk=pk)
    
    # Foydalanuvchi shu kursga yozilganmi?
    if not request.user.is_admin and not request.user.is_teacher:
        if not CourseEnrollment.objects.filter(user=request.user, profession=profession).exists():
            messages.error(request, "Avval kursga yoziling!")
            return redirect('profession_detail', pk=pk)
    
    # Kursdoshlar
    classmates = CustomUser.objects.filter(
        enrollments__profession=profession,
        role='student'
    ).exclude(pk=request.user.pk).order_by('-coins', 'first_name')
    
    return render(request, 'accounts/classmates.html', {
        'profession': profession,
        'classmates': classmates,
    })


@login_required
def student_public_profile(request, pk):
    """Boshqa o'quvchining umumiy profili"""
    student = get_object_or_404(CustomUser, pk=pk)
    
    # Faqat o'quvchilar ko'rinishi mumkin
    if student.role != 'student':
        messages.error(request, "Bu sahifa mavjud emas!")
        return redirect('home')
    
    # Umumiy kurslar
    user_profession_ids = request.user.enrollments.values_list('profession_id', flat=True)
    student_enrollments = CourseEnrollment.objects.filter(user=student).select_related('profession')
    
    common_courses = []
    for enrollment in student_enrollments:
        if enrollment.profession_id in user_profession_ids:
            common_courses.append(enrollment.profession)
    
    # Sertifikatlar
    certificates = Certificate.objects.filter(student=student).select_related('profession')
    
    # Tugallangan kurslar soni
    completed_count = certificates.count()
    
    return render(request, 'accounts/student_public_profile.html', {
        'student': student,
        'common_courses': common_courses,
        'certificates': certificates,
        'completed_count': completed_count,
    })


@login_required
def profile_edit_view(request):
    if request.method == 'POST':
        form = UserProfileForm(request.POST, request.FILES, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "Profil muvaffaqiyatli yangilandi!")
            return redirect('profile')
    else:
        form = UserProfileForm(instance=request.user)
    
    return render(request, 'accounts/profile_edit.html', {'form': form})


@login_required
def change_password_view(request):
    if request.method == 'POST':
        form = ChangePasswordForm(request.POST)
        if form.is_valid():
            if not request.user.check_password(form.cleaned_data['old_password']):
                messages.error(request, "Joriy parol noto'g'ri!")
            else:
                request.user.set_password(form.cleaned_data['new_password1'])
                request.user.save()
                login(request, request.user)
                messages.success(request, "Parol muvaffaqiyatli o'zgartirildi!")
                return redirect('profile')
    else:
        form = ChangePasswordForm()
    
    return render(request, 'accounts/change_password.html', {'form': form})


@login_required
def student_statistics(request, pk=None):
    if pk and (request.user.is_admin or request.user.is_teacher):
        student = get_object_or_404(CustomUser, pk=pk)
    else:
        student = request.user
    
    period = request.GET.get('period', 'all')
    now = timezone.now()
    
    if period == 'week':
        start_date = now - timedelta(days=7)
    elif period == 'month':
        start_date = now - timedelta(days=30)
    else:
        start_date = None
    
    # Videos watched
    videos_query = VideoProgress.objects.filter(user=student, watched=True)
    if start_date:
        videos_query = videos_query.filter(watched_at__gte=start_date)
    videos_watched = videos_query.count()
    
    # Test results
    tests_query = TestResult.objects.filter(student=student)
    if start_date:
        tests_query = tests_query.filter(completed_at__gte=start_date)
    test_results = tests_query
    avg_score = tests_query.aggregate(avg=Avg('score'))['avg'] or 0
    
    # Homework submissions
    hw_query = HomeworkSubmission.objects.filter(student=student)
    if start_date:
        hw_query = hw_query.filter(submitted_at__gte=start_date)
    homework_count = hw_query.count()
    
    context = {
        'student': student,
        'period': period,
        'videos_watched': videos_watched,
        'test_results': test_results,
        'avg_score': round(avg_score, 1),
        'homework_count': homework_count,
        'coins': student.coins,
    }
    
    return render(request, 'accounts/student_statistics.html', context)


@login_required
def export_student_pdf(request, pk=None):
    if pk and (request.user.is_admin or request.user.is_teacher):
        student = get_object_or_404(CustomUser, pk=pk)
    else:
        student = request.user
    
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from io import BytesIO
    
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    elements = []
    styles = getSampleStyleSheet()
    
    elements.append(Paragraph(f"O'quvchi statistikasi: {student.full_name}", styles['Heading1']))
    elements.append(Paragraph(f"Sana: {timezone.now().strftime('%d.%m.%Y')}", styles['Normal']))
    elements.append(Spacer(1, 20))
    
    # General info
    info_data = [
        ['Ko\'rsatkich', 'Qiymat'],
        ['Jami coinlar', str(student.coins)],
        ['Ko\'rilgan videolar', str(VideoProgress.objects.filter(user=student, watched=True).count())],
        ['Test natijalari', str(TestResult.objects.filter(student=student).count())],
        ['Topshirilgan vazifalar', str(HomeworkSubmission.objects.filter(student=student).count())],
    ]
    
    table = Table(info_data, colWidths=[250, 150])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0d6efd')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#dee2e6')),
        ('PADDING', (0, 0), (-1, -1), 8),
    ]))
    elements.append(table)
    
    doc.build(elements)
    buffer.seek(0)
    
    response = HttpResponse(buffer, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="student_stats_{student.username}.pdf"'
    return response


@login_required
def export_system_pdf(request):
    if not request.user.is_admin:
        messages.error(request, "Bu sahifaga kirish huquqingiz yo'q!")
        return redirect('home')
    
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from io import BytesIO
    
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=30, bottomMargin=30)
    elements = []
    styles = getSampleStyleSheet()
    
    heading_style = ParagraphStyle(
        'CustomHeading',
        parent=styles['Heading2'],
        spaceAfter=10,
        spaceBefore=20,
        textColor=colors.HexColor('#0d6efd')
    )
    
    def create_table(data, col_widths=None):
        if col_widths is None:
            col_widths = [300, 150]
        table = Table(data, colWidths=col_widths)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0d6efd')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#dee2e6')),
            ('PADDING', (0, 0), (-1, -1), 8),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8f9fa')]),
        ]))
        return table
    
    elements.append(Paragraph("Tizim Statistikasi - To'liq Hisobot", styles['Title']))
    elements.append(Paragraph(f"Hisobot sanasi: {timezone.now().strftime('%d.%m.%Y %H:%M')}", styles['Normal']))
    elements.append(Spacer(1, 20))
    
    total_students = CustomUser.objects.filter(role='student').count()
    total_teachers = CustomUser.objects.filter(role='teacher').count()
    total_admins = CustomUser.objects.filter(role='admin').count()
    total_users = CustomUser.objects.count()
    
    elements.append(Paragraph("1. Foydalanuvchilar statistikasi", heading_style))
    user_data = [
        ["Ko'rsatkich", 'Soni'],
        ['Jami foydalanuvchilar', str(total_users)],
        ["O'quvchilar", str(total_students)],
        ["O'qituvchilar", str(total_teachers)],
        ['Adminlar', str(total_admins)],
    ]
    elements.append(create_table(user_data))
    elements.append(Spacer(1, 15))
    
    total_courses = Profession.objects.count()
    courses_with_students = Profession.objects.annotate(
        student_count=Count('enrollments')
    ).order_by('-student_count')
    
    elements.append(Paragraph("2. Kurslar va talabalar", heading_style))
    course_data = [['Kurs nomi', "O'quvchilar soni"]]
    for course in courses_with_students[:10]:
        course_data.append([course.name[:40], str(course.student_count)])
    if total_courses > 10:
        course_data.append(['...va yana', f'{total_courses - 10} ta kurs'])
    elements.append(create_table(course_data))
    elements.append(Spacer(1, 15))
    
    total_lessons = Lesson.objects.count()
    total_videos = VideoLesson.objects.count()
    total_tests = Test.objects.count()
    total_homeworks_created = Homework.objects.count()
    
    elements.append(Paragraph("3. Dars materiallari", heading_style))
    lesson_data = [
        ["Ko'rsatkich", 'Soni'],
        ['Jami darslar', str(total_lessons)],
        ['Videodarslar', str(total_videos)],
        ['Testlar', str(total_tests)],
        ['Uy vazifalari', str(total_homeworks_created)],
    ]
    elements.append(create_table(lesson_data))
    elements.append(Spacer(1, 15))
    
    total_test_results = TestResult.objects.count()
    passed_tests = TestResult.objects.filter(passed=True).count()
    failed_tests = total_test_results - passed_tests
    avg_score = TestResult.objects.aggregate(avg=Avg('score'))['avg'] or 0
    
    elements.append(Paragraph("4. Test natijalari", heading_style))
    test_data = [
        ["Ko'rsatkich", 'Qiymat'],
        ["Jami topshirilgan testlar", str(total_test_results)],
        ["Muvaffaqiyatli o'tganlar", str(passed_tests)],
        ["Muvaffaqiyatsiz", str(failed_tests)],
        ["O'rtacha ball", f"{avg_score:.1f}%"],
    ]
    elements.append(create_table(test_data))
    elements.append(Spacer(1, 15))
    
    total_submissions = HomeworkSubmission.objects.count()
    pending_homeworks = HomeworkSubmission.objects.filter(grade__isnull=True).count()
    graded_homeworks = HomeworkSubmission.objects.filter(grade__isnull=False).count()
    avg_grade = HomeworkSubmission.objects.filter(grade__isnull=False).aggregate(avg=Avg('grade'))['avg'] or 0
    
    elements.append(Paragraph("5. Uy vazifalari holati", heading_style))
    hw_data = [
        ["Ko'rsatkich", 'Qiymat'],
        ['Jami topshirilgan vazifalar', str(total_submissions)],
        ['Baholanmagan (kutilmoqda)', str(pending_homeworks)],
        ['Baholangan', str(graded_homeworks)],
        ["O'rtacha baho", f"{avg_grade:.1f}"],
    ]
    elements.append(create_table(hw_data))
    elements.append(Spacer(1, 15))
    
    total_coins = CustomUser.objects.aggregate(Sum('coins'))['coins__sum'] or 0
    total_transactions = CoinTransaction.objects.count()
    avg_coins_per_user = total_coins / total_users if total_users > 0 else 0
    
    elements.append(Paragraph("6. Coin statistikasi", heading_style))
    coin_data = [
        ["Ko'rsatkich", 'Qiymat'],
        ['Jami tarqatilgan coinlar', str(total_coins)],
        ['Coin tranzaksiyalari', str(total_transactions)],
        ["Har bir foydalanuvchiga o'rtacha", f"{avg_coins_per_user:.1f}"],
    ]
    elements.append(create_table(coin_data))
    elements.append(Spacer(1, 15))
    
    total_enrollments = CourseEnrollment.objects.count()
    
    elements.append(Paragraph("7. Kursga yozilish statistikasi", heading_style))
    enroll_data = [
        ["Ko'rsatkich", 'Soni'],
        ["Jami yozilishlar", str(total_enrollments)],
    ]
    elements.append(create_table(enroll_data))
    elements.append(Spacer(1, 15))
    
    elements.append(Paragraph("8. Oxirgi 7 kunlik registratsiya", heading_style))
    reg_data = [['Sana', "Yangi foydalanuvchilar"]]
    for i in range(6, -1, -1):
        day = timezone.now().date() - timedelta(days=i)
        count = CustomUser.objects.filter(
            date_joined__date=day
        ).count()
        reg_data.append([day.strftime('%d.%m.%Y'), str(count)])
    elements.append(create_table(reg_data))
    
    doc.build(elements)
    buffer.seek(0)
    
    response = HttpResponse(buffer, content_type='application/pdf')
    response['Content-Disposition'] = 'attachment; filename="system_statistics.pdf"'
    return response


@login_required
def leaderboard(request):
    students = CustomUser.objects.filter(role='student').order_by('-coins')[:50]
    return render(request, 'accounts/leaderboard.html', {'students': students})


# Messages views
@login_required
def messages_view(request):
    # Shaxsiy xabarlar
    personal_messages = Message.objects.filter(recipient=request.user)
    
    # Umumiy xabarlar
    if request.user.is_student:
        general_messages = Message.objects.filter(
            Q(message_type='all') | Q(message_type='students'),
            recipient__isnull=True
        )
    elif request.user.is_teacher:
        general_messages = Message.objects.filter(
            Q(message_type='all') | Q(message_type='teachers'),
            recipient__isnull=True
        )
    else:
        general_messages = Message.objects.filter(message_type='all', recipient__isnull=True)
    
    all_messages = (personal_messages | general_messages).distinct().order_by('-created_at')
    
    # O'qilmagan xabarlar sonini yangilash
    unread_count = all_messages.filter(is_read=False).count()
    
    return render(request, 'accounts/messages.html', {
        'messages_list': all_messages,
        'unread_count': unread_count,
    })


@login_required
def message_detail(request, pk):
    message = get_object_or_404(Message, pk=pk)
    
    # Faqat o'ziga tegishli xabarlarni ko'rish
    if message.recipient and message.recipient != request.user:
        if not request.user.is_admin:
            messages.error(request, "Bu xabarga kirish huquqingiz yo'q!")
            return redirect('messages')
    
    # O'qilgan deb belgilash
    if not message.is_read and message.recipient == request.user:
        message.is_read = True
        message.save()
    
    return render(request, 'accounts/message_detail.html', {'message': message})


@login_required
def mark_message_read(request, pk):
    message = get_object_or_404(Message, pk=pk, recipient=request.user)
    message.is_read = True
    message.save()
    return JsonResponse({'success': True})


# Admin: Send message
@login_required
def admin_send_message(request):
    if not request.user.is_admin:
        return redirect('home')
    
    if request.method == 'POST':
        title = request.POST.get('title')
        content = request.POST.get('content')
        message_type = request.POST.get('message_type')
        recipient_id = request.POST.get('recipient')
        
        if message_type == 'personal' and recipient_id:
            recipient = get_object_or_404(CustomUser, pk=recipient_id)
            Message.objects.create(
                title=title,
                content=content,
                message_type='personal',
                recipient=recipient,
                sender=request.user
            )
            messages.success(request, f"{recipient.full_name}ga xabar yuborildi!")
        else:
            # Umumiy xabar
            if message_type in ['students', 'teachers']:
                role = 'student' if message_type == 'students' else 'teacher'
                users = CustomUser.objects.filter(role=role)
                for user in users:
                    Message.objects.create(
                        title=title,
                        content=content,
                        message_type=message_type,
                        recipient=user,
                        sender=request.user
                    )
            else:
                # Barchaga
                users = CustomUser.objects.exclude(role='admin')
                for user in users:
                    Message.objects.create(
                        title=title,
                        content=content,
                        message_type='all',
                        recipient=user,
                        sender=request.user
                    )
            messages.success(request, "Xabar muvaffaqiyatli yuborildi!")
        
        return redirect('admin_messages')
    
    users = CustomUser.objects.exclude(role='admin')
    return render(request, 'accounts/admin/send_message.html', {'users': users})


@login_required
def admin_messages(request):
    if not request.user.is_admin:
        return redirect('home')
    
    all_messages = Message.objects.all().order_by('-created_at')[:100]
    return render(request, 'accounts/admin/messages.html', {'messages_list': all_messages})

from django.core.paginator import Paginator

# Admin: Payment management
@login_required
def admin_payments(request):
    if not request.user.is_admin:
        return redirect('home')

    qs = CustomUser.objects.filter(role='student')

    for s in qs:
        PaymentStatus.objects.get_or_create(user=s)

    qs = CustomUser.objects.filter(role='student').select_related('payment_status')

    paginator = Paginator(qs, 10)  # har sahifada 10 ta
    page = request.GET.get('page')
    students = paginator.get_page(page)

    blocked_students = qs.filter(is_blocked=True).count()
    today = timezone.now()

    return render(request, 'accounts/admin/payments.html', {
        'students': students,
        'blocked_students': blocked_students,
        'today': today,
    })


@login_required
def admin_mark_paid(request, pk):
    if not request.user.is_admin:
        return redirect('home')
    
    user = get_object_or_404(CustomUser, pk=pk)
    payment_status, _ = PaymentStatus.objects.get_or_create(user=user)
    payment_status.is_paid = True
    payment_status.last_payment_date = timezone.now().date()
    payment_status.save()
    
    # Agar bloklangan bo'lsa, ochish
    if user.is_blocked and payment_status.auto_blocked:
        user.is_blocked = False
        user.save()
        payment_status.auto_blocked = False
        payment_status.save()
        
        Message.objects.create(
            title="✅ Hisobingiz ochildi",
            content="To'lov qabul qilindi. Hisobingiz faollashtirildi. O'qishingizni davom eting!",
            message_type='system',
            recipient=user,
            sender=request.user
        )
    
    messages.success(request, f"{user.full_name} to'lovi belgilandi!")
    return redirect('admin_payments')


@login_required
def send_payment_reminder(request, pk):
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kirish huquqi yo'q!")
        return redirect('home')
    
    student = get_object_or_404(CustomUser, pk=pk, role='student')
    
    Message.objects.create(
        sender=request.user,
        recipient=student,
        title="To'lov eslatmasi",
        content=f"Hurmatli {student.full_name},\n\nSizning oylik to'lovingiz muddati yaqinlashmoqda. Iltimos, o'z vaqtida to'lov qilishni unutmang.\n\nHurmat bilan,\nIT Creative jamoasi",
        message_type='payment'
    )
    
    messages.success(request, f"{student.full_name}ga to'lov eslatmasi yuborildi!")
    return redirect('admin_payments')


@login_required
def send_bulk_payment_reminders(request):
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kirish huquqi yo'q!")
        return redirect('home')
    
    if request.method == 'POST':
        unpaid_students = CustomUser.objects.filter(role='student').exclude(
            payment_status__is_paid=True
        )
        
        count = 0
        for student in unpaid_students:
            Message.objects.create(
                sender=request.user,
                recipient=student,
                title="To'lov eslatmasi",
                content=f"Hurmatli {student.full_name},\n\nSizning oylik to'lovingiz muddati yaqinlashmoqda. Iltimos, o'z vaqtida to'lov qilishni unutmang.\n\nHurmat bilan,\nIT Creative jamoasi",
                message_type='payment'
            )
            count += 1
        
        messages.success(request, f"{count} ta o'quvchiga to'lov eslatmasi yuborildi!")
    
    return redirect('admin_payments')


# Teacher/Admin: Manage lessons
@login_required
def manage_lessons(request, pk):
    if not (request.user.is_admin or request.user.is_teacher):
        messages.error(request, "Sizda bu sahifaga kirish huquqi yo'q!")
        return redirect('home')
    
    profession = get_object_or_404(Profession, pk=pk)
    sections = profession.sections.all().prefetch_related('lessons')
    lessons_without_section = profession.lessons.filter(section__isnull=True)
    
    return render(request, 'accounts/manage/lessons.html', {
        'profession': profession,
        'sections': sections,
        'lessons_without_section': lessons_without_section,
    })


@login_required
def add_lesson(request, pk):
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')
    
    profession = get_object_or_404(Profession, pk=pk)
    lesson_type = request.GET.get('type', 'video')
    section_id = request.GET.get('section')
    section = None
    
    if section_id:
        section = get_object_or_404(Section, pk=section_id, profession=profession)
    
    if request.method == 'POST':
        section_id_post = request.POST.get('section')
        if section_id_post:
            section = get_object_or_404(Section, pk=section_id_post, profession=profession)
        
        if lesson_type == 'video':
            form = VideoLessonForm(request.POST)
            if form.is_valid():
                lesson = Lesson.objects.create(
                    profession=profession,
                    section=section,
                    title=form.cleaned_data['title'],
                    lesson_type='video',
                    created_by=request.user
                )
                video_url = form.cleaned_data.get('video_url') or ''
                VideoLesson.objects.create(
                    lesson=lesson,
                    video_url=video_url,
                    youtube_url=video_url if ('youtube.com' in video_url or 'youtu.be' in video_url) else '',
                    duration=form.cleaned_data.get('duration') or 0
                )
                # Xabar yuborish
                send_course_notification(
                    profession=profession,
                    title=f"🎬 Yangi video dars: {lesson.title}",
                    content=f"'{profession.name}' kursiga yangi video dars qo'shildi:\n\n📚 {lesson.title}\n\nDarsni ko'rish uchun kursga kiring!",
                    sender=request.user
                )
                messages.success(request, "Video darslik qo'shildi!")
                return redirect('profession_detail', pk=pk)
        
        elif lesson_type == 'homework':
            form = HomeworkForm(request.POST)
            if form.is_valid():
                lesson = Lesson.objects.create(
                    profession=profession,
                    section=section,
                    title=form.cleaned_data['title'],
                    lesson_type='homework',
                    created_by=request.user
                )
                Homework.objects.create(
                    lesson=lesson,
                    description=form.cleaned_data['description']
                )
                # Xabar yuborish
                send_course_notification(
                    profession=profession,
                    title=f"📝 Yangi uyga vazifa: {lesson.title}",
                    content=f"'{profession.name}' kursiga yangi uyga vazifa qo'shildi:\n\n📚 {lesson.title}\n\nVazifani bajarish uchun kursga kiring!",
                    sender=request.user
                )
                messages.success(request, "Uyga vazifa qo'shildi!")
                return redirect('profession_detail', pk=pk)
        
        elif lesson_type == 'test':
            form = TestForm(request.POST)
            if form.is_valid():
                lesson = Lesson.objects.create(
                    profession=profession,
                    section=section,
                    title=form.cleaned_data['title'],
                    lesson_type='test',
                    created_by=request.user
                )
                Test.objects.create(
                    lesson=lesson,
                    test_type=form.cleaned_data['test_type'],
                    time_limit=form.cleaned_data['time_limit'],
                    passing_score=form.cleaned_data['passing_score'],
                    allow_retry=form.cleaned_data.get('allow_retry', False)
                )
                # Xabar yuborish
                send_course_notification(
                    profession=profession,
                    title=f"📋 Yangi test: {lesson.title}",
                    content=f"'{profession.name}' kursiga yangi test qo'shildi:\n\n📚 {lesson.title}\n\nTestni yechish uchun kursga kiring!",
                    sender=request.user
                )
                messages.success(request, "Test qo'shildi! Endi savollarni qo'shing.")
                return redirect('manage_test_questions', pk=lesson.test.pk)
    else:
        if lesson_type == 'video':
            form = VideoLessonForm()
        elif lesson_type == 'homework':
            form = HomeworkForm()
        else:
            form = TestForm()
    
    sections = profession.sections.filter(is_active=True)
    
    return render(request, 'accounts/manage/add_lesson.html', {
        'profession': profession,
        'lesson_type': lesson_type,
        'form': form,
        'section': section,
        'sections': sections,
    })


@login_required
def edit_lesson(request, pk):
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')
    
    lesson = get_object_or_404(Lesson, pk=pk)
    
    if request.method == 'POST':
        if lesson.lesson_type == 'video':
            form = VideoLessonForm(request.POST)
            if form.is_valid():
                lesson.title = form.cleaned_data['title']
                lesson.save()
                lesson.video.youtube_url = form.cleaned_data['youtube_url']
                lesson.video.duration = form.cleaned_data.get('duration') or 0
                lesson.video.save()
                # Xabar yuborish
                send_course_notification(
                    profession=lesson.profession,
                    title=f"🔄 Video dars yangilandi: {lesson.title}",
                    content=f"'{lesson.profession.name}' kursidagi video dars yangilandi:\n\n📚 {lesson.title}\n\nYangilangan darsni ko'rish uchun kursga kiring!",
                    sender=request.user
                )
                messages.success(request, "Dars yangilandi!")
                return redirect('manage_lessons', pk=lesson.profession.pk)
        elif lesson.lesson_type == 'homework':
            form = HomeworkForm(request.POST)
            if form.is_valid():
                lesson.title = form.cleaned_data['title']
                lesson.save()
                lesson.homework.description = form.cleaned_data['description']
                lesson.homework.save()
                # Xabar yuborish
                send_course_notification(
                    profession=lesson.profession,
                    title=f"🔄 Uyga vazifa yangilandi: {lesson.title}",
                    content=f"'{lesson.profession.name}' kursidagi uyga vazifa yangilandi:\n\n📚 {lesson.title}\n\nVazifani ko'rish uchun kursga kiring!",
                    sender=request.user
                )
                messages.success(request, "Vazifa yangilandi!")
                return redirect('manage_lessons', pk=lesson.profession.pk)
    else:
        if lesson.lesson_type == 'video':
            form = VideoLessonForm(initial={
                'title': lesson.title,
                'youtube_url': lesson.video.youtube_url,
                'duration': lesson.video.duration,
            })
        elif lesson.lesson_type == 'homework':
            form = HomeworkForm(initial={
                'title': lesson.title,
                'description': lesson.homework.description,
            })
        else:
            return redirect('manage_test_questions', pk=lesson.test.pk)
    
    return render(request, 'accounts/manage/edit_lesson.html', {
        'lesson': lesson,
        'form': form,
    })


@login_required
def delete_lesson(request, pk):
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')
    
    lesson = get_object_or_404(Lesson, pk=pk)
    profession = lesson.profession
    profession_pk = profession.pk
    lesson_title = lesson.title
    lesson_type = lesson.lesson_type
    
    if request.method == 'POST':
        # Xabar yuborish
        type_names = {'video': 'Video dars', 'homework': 'Uyga vazifa', 'test': 'Test'}
        type_name = type_names.get(lesson_type, 'Dars')
        send_course_notification(
            profession=profession,
            title=f"🗑️ {type_name} o'chirildi",
            content=f"'{profession.name}' kursidan quyidagi dars o'chirildi:\n\n📚 {lesson_title}\n\nBu dars endi mavjud emas.",
            sender=request.user
        )
        lesson.delete()
        messages.success(request, "Dars o'chirildi!")
        return redirect('manage_lessons', pk=profession_pk)
    
    return render(request, 'accounts/manage/delete_lesson.html', {'lesson': lesson})


# Test management
@login_required
def manage_test_questions(request, pk):
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')
    
    test = get_object_or_404(Test, pk=pk)
    questions = test.questions.all()
    
    return render(request, 'accounts/manage/test_questions.html', {
        'test': test,
        'questions': questions,
    })


@login_required
def add_test_question(request, pk):
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')
    
    test = get_object_or_404(Test, pk=pk)
    
    if request.method == 'POST':
        form = TestQuestionForm(request.POST, request.FILES)
        if form.is_valid():
            question = TestQuestion.objects.create(
                test=test,
                question_text=form.cleaned_data['question_text'],
                question_image=form.cleaned_data.get('question_image')
            )
            
            answers = [
                form.cleaned_data['answer1'],
                form.cleaned_data['answer2'],
                form.cleaned_data['answer3'],
            ]
            correct = int(form.cleaned_data['correct_answer'])
            
            for i, answer_text in enumerate(answers, 1):
                TestAnswer.objects.create(
                    question=question,
                    answer_text=answer_text,
                    is_correct=(i == correct)
                )
            
            messages.success(request, "Savol qo'shildi!")
            
            if 'add_another' in request.POST:
                return redirect('add_test_question', pk=pk)
            return redirect('manage_test_questions', pk=pk)
    else:
        form = TestQuestionForm()
    
    return render(request, 'accounts/manage/add_question.html', {
        'test': test,
        'form': form,
    })


@login_required
def delete_test_question(request, pk):
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')
    
    question = get_object_or_404(TestQuestion, pk=pk)
    test_pk = question.test.pk
    
    if request.method == 'POST':
        question.delete()
        messages.success(request, "Savol o'chirildi!")
    
    return redirect('manage_test_questions', pk=test_pk)


# Homework submissions for teachers
@login_required
def homework_submissions(request):
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')
    
    submissions = HomeworkSubmission.objects.filter(status='pending').select_related(
        'homework__lesson__profession', 'student'
    )
    
    return render(request, 'accounts/manage/homework_submissions.html', {
        'submissions': submissions,
    })


@login_required
def grade_homework(request, pk):
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')
    
    submission = get_object_or_404(HomeworkSubmission, pk=pk)
    was_graded = submission.grade is not None
    
    if request.method == 'POST':
        form = HomeworkGradeForm(request.POST)
        if form.is_valid():
            submission.grade = form.cleaned_data['grade']
            submission.feedback = form.cleaned_data.get('feedback', '')
            submission.status = 'graded'
            submission.graded_by = request.user
            submission.graded_at = timezone.now()
            submission.save()
            
            # Baholangan vazifa uchun 5 coin (faqat birinchi marta)
            if not was_graded and not submission.coin_awarded:
                submission.student.add_coins(5, f"Vazifa baholandi: {submission.homework.lesson.title}")
                submission.coin_awarded = True
                submission.save()
                
                # Activity log
                ActivityLog.objects.create(
                    user=submission.student,
                    action_type='Vazifa baxosi uchun',
                    description=f"Vazifasi baholandi: {submission.homework.lesson.title} - Baho: {submission.grade}, +5 coin"
                )
            
            messages.success(request, "Vazifa baholandi!")
            return redirect('homework_submissions')
    else:
        form = HomeworkGradeForm()
    
    return render(request, 'accounts/manage/grade_homework.html', {
        'submission': submission,
        'form': form,
    })


# Test results for teachers
@login_required
def all_test_results(request):
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')
    
    results = TestResult.objects.all().select_related('test__lesson__profession', 'student')
    
    return render(request, 'accounts/manage/test_results.html', {'results': results})


# Certificates
@login_required
def issue_certificate(request, pk):
    if not request.user.is_admin:
        return redirect('home')
    
    student = get_object_or_404(CustomUser, pk=pk)
    professions = Profession.objects.all()
    
    if request.method == 'POST':
        form = CertificateForm(request.POST, request.FILES)
        profession_id = request.POST.get('profession')
        
        if form.is_valid() and profession_id:
            profession = get_object_or_404(Profession, pk=profession_id)
            cert = form.save(commit=False)
            cert.student = student
            cert.profession = profession
            cert.issued_by = request.user
            cert.save()
            messages.success(request, "Sertifikat berildi!")
            return redirect('admin_user_view', pk=pk)
    else:
        form = CertificateForm()
    
    return render(request, 'accounts/manage/issue_certificate.html', {
        'student': student,
        'professions': professions,
        'form': form,
    })


# Admin Panel Views
@login_required
def admin_dashboard(request):
    if not request.user.is_admin:
        messages.error(request, "Sizda admin paneliga kirish huquqi yo'q!")
        return redirect('home')
    
    total_users = CustomUser.objects.count()
    total_teachers = CustomUser.objects.filter(role='teacher').count()
    total_students = CustomUser.objects.filter(role='student').count()
    total_professions = Profession.objects.count()
    blocked_users = CustomUser.objects.filter(is_blocked=True).count()
    online_users = sum(1 for u in CustomUser.objects.all() if u.is_online)
    
    # Qo'shimcha statistika
    total_lessons = Lesson.objects.count()
    total_videos = VideoLesson.objects.count()
    total_tests = Test.objects.count()
    total_homeworks = Homework.objects.count()
    total_enrollments = CourseEnrollment.objects.count()
    total_certificates = Certificate.objects.count()
    total_coins = CustomUser.objects.aggregate(total=Sum('coins'))['total'] or 0
    
    # Test natijalari
    total_test_results = TestResult.objects.count()
    passed_tests = TestResult.objects.filter(passed=True).count()
    avg_test_score = TestResult.objects.aggregate(avg=Avg('score'))['avg'] or 0
    
    # Uyga vazifalar
    pending_homeworks = HomeworkSubmission.objects.filter(status='pending').count()
    graded_homeworks = HomeworkSubmission.objects.filter(status='graded').count()
    
    # Mahsulot statistikasi
    from coin.models import Product, ProductPurchase
    total_products = Product.objects.count()
    active_products = Product.objects.filter(is_active=True).count()
    total_purchases = ProductPurchase.objects.count()
    total_coins_spent = ProductPurchase.objects.aggregate(total=Sum('coins_spent'))['total'] or 0
    
    profession_stats = Profession.objects.annotate(
        student_count=Count('enrollments', filter=Q(enrollments__user__role='student')),
        teacher_count=Count('students', filter=Q(students__role='teacher'))
    )
    
    recent_users = CustomUser.objects.filter(last_activity__isnull=False).order_by('-last_activity')[:10]
    
    # Oxirgi 7 kun statistikasi
    today = timezone.now().date()
    daily_stats = []
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        daily_stats.append({
            'date': day,
            'users': CustomUser.objects.filter(date_joined__date=day).count(),
            'enrollments': CourseEnrollment.objects.filter(enrolled_at__date=day).count(),
        })
    
    context = {
        'total_users': total_users,
        'total_teachers': total_teachers,
        'total_students': total_students,
        'total_professions': total_professions,
        'blocked_users': blocked_users,
        'online_users': online_users,
        'total_lessons': total_lessons,
        'total_videos': total_videos,
        'total_tests': total_tests,
        'total_homeworks': total_homeworks,
        'total_enrollments': total_enrollments,
        'total_certificates': total_certificates,
        'total_coins': total_coins,
        'total_test_results': total_test_results,
        'passed_tests': passed_tests,
        'avg_test_score': round(avg_test_score, 1),
        'pending_homeworks': pending_homeworks,
        'graded_homeworks': graded_homeworks,
        'profession_stats': profession_stats,
        'recent_users': recent_users,
        'daily_stats': daily_stats,
        'total_products': total_products,
        'active_products': active_products,
        'total_purchases': total_purchases,
        'total_coins_spent': total_coins_spent,
    }
    return render(request, 'accounts/admin/dashboard.html', context)


@login_required
def admin_professions(request):
    if not request.user.is_admin:
        return redirect('home')
    
    professions = Profession.objects.annotate(
        student_count=Count('enrollments', filter=Q(enrollments__user__role='student')),
        teacher_count=Count('students', filter=Q(students__role='teacher'))
    )
    return render(request, 'accounts/admin/professions.html', {'professions': professions})


@login_required
def admin_profession_add(request):
    if not request.user.is_admin:
        return redirect('home')
    
    if request.method == 'POST':
        form = ProfessionForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            messages.success(request, "Kasb muvaffaqiyatli qo'shildi!")
            return redirect('admin_professions')
    else:
        form = ProfessionForm()
    
    return render(request, 'accounts/admin/profession_form.html', {'form': form, 'title': "Kasb qo'shish"})


@login_required
def admin_profession_edit(request, pk):
    if not request.user.is_admin:
        return redirect('home')
    
    profession = get_object_or_404(Profession, pk=pk)
    
    if request.method == 'POST':
        form = ProfessionForm(request.POST, request.FILES, instance=profession)
        if form.is_valid():
            form.save()
            messages.success(request, "Kasb muvaffaqiyatli tahrirlandi!")
            return redirect('admin_professions')
    else:
        form = ProfessionForm(instance=profession)
    
    return render(request, 'accounts/admin/profession_form.html', {'form': form, 'title': "Kasbni tahrirlash", 'profession': profession})


@login_required
def admin_profession_delete(request, pk):
    if not request.user.is_admin:
        return redirect('home')
    
    profession = get_object_or_404(Profession, pk=pk)
    
    if request.method == 'POST':
        profession.delete()
        messages.success(request, "Kasb muvaffaqiyatli o'chirildi!")
        return redirect('admin_professions')
    
    return render(request, 'accounts/admin/profession_delete.html', {'profession': profession})


@login_required
def admin_users(request):
    if not request.user.is_admin:
        return redirect('home')
    
    role_filter = request.GET.get('role', '')
    profession_filter = request.GET.get('profession', '')
    status_filter = request.GET.get('status', '')
    
    users = CustomUser.objects.all()
    
    if role_filter:
        users = users.filter(role=role_filter)
    if profession_filter:
        users = users.filter(profession_id=profession_filter)
    if status_filter == 'blocked':
        users = users.filter(is_blocked=True)
    elif status_filter == 'active':
        users = users.filter(is_blocked=False)
    
    professions = Profession.objects.all()
    
    return render(request, 'accounts/admin/users.html', {
        'users': users,
        'professions': professions,
        'role_filter': role_filter,
        'profession_filter': profession_filter,
        'status_filter': status_filter,
    })


@login_required
def admin_user_view(request, pk):
    if not request.user.is_admin:
        return redirect('home')
    
    user = get_object_or_404(CustomUser, pk=pk)
    certificates = Certificate.objects.filter(student=user)
    
    return render(request, 'accounts/admin/user_view.html', {
        'user_obj': user,
        'certificates': certificates,
    })


@login_required
def admin_user_edit(request, pk):
    if not request.user.is_admin:
        return redirect('home')
    
    user = get_object_or_404(CustomUser, pk=pk)
    
    if request.method == 'POST':
        form = AdminUserEditForm(request.POST, request.FILES, instance=user)
        if form.is_valid():
            user = form.save()
            new_password = request.POST.get('new_password')
            if new_password:
                user.set_password(new_password)
                user.save()
            messages.success(request, "Foydalanuvchi muvaffaqiyatli yangilandi!")
            return redirect('admin_users')
    else:
        form = AdminUserEditForm(instance=user)
    
    return render(request, 'accounts/admin/user_edit.html', {'form': form, 'user_obj': user})


@login_required
def admin_user_block(request, pk):
    if not request.user.is_admin:
        return redirect('home')
    
    user = get_object_or_404(CustomUser, pk=pk)
    
    if user.is_admin:
        messages.error(request, "Admin foydalanuvchini bloklash mumkin emas!")
        return redirect('admin_users')
    
    user.is_blocked = not user.is_blocked
    user.save()
    
    messages.success(request, f"{user.full_name} {'bloklandi' if user.is_blocked else 'blokdan chiqarildi'}!")
    return redirect('admin_users')


@login_required
def admin_user_delete(request, pk):
    if not request.user.is_admin:
        return redirect('home')
    
    user = get_object_or_404(CustomUser, pk=pk)
    
    if user.is_admin:
        messages.error(request, "Admin foydalanuvchini o'chirish mumkin emas!")
        return redirect('admin_users')
    
    if request.method == 'POST':
        user.delete()
        messages.success(request, "Foydalanuvchi o'chirildi!")
        return redirect('admin_users')
    
    return render(request, 'accounts/admin/user_delete.html', {'user_obj': user})


@login_required
def admin_statistics(request):
    if not request.user.is_admin:
        return redirect('home')
    
    total_users = CustomUser.objects.count()
    total_teachers = CustomUser.objects.filter(role='teacher').count()
    total_students = CustomUser.objects.filter(role='student').count()
    total_admins = CustomUser.objects.filter(role='admin').count()
    blocked_users = CustomUser.objects.filter(is_blocked=True).count()
    online_users = sum(1 for u in CustomUser.objects.all() if u.is_online)
    
    profession_stats = Profession.objects.annotate(
        student_count=Count('enrollments', filter=Q(enrollments__user__role='student')),
        teacher_count=Count('students', filter=Q(students__role='teacher')),
        total=Count('enrollments')
    )
    
    from datetime import timedelta
    today = timezone.now().date()
    daily_registrations = []
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        count = CustomUser.objects.filter(date_joined__date=day).count()
        daily_registrations.append({'date': day, 'count': count})
    
    return render(request, 'accounts/admin/statistics.html', {
        'total_users': total_users,
        'total_teachers': total_teachers,
        'total_students': total_students,
        'total_admins': total_admins,
        'blocked_users': blocked_users,
        'online_users': online_users,
        'profession_stats': profession_stats,
        'daily_registrations': daily_registrations,
    })


@login_required
def admin_export_pdf(request):
    if not request.user.is_admin:
        return redirect('home')
    
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from io import BytesIO
    
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    elements = []
    styles = getSampleStyleSheet()
    
    elements.append(Paragraph("LMS Statistika Hisoboti", styles['Heading1']))
    elements.append(Paragraph(f"Sana: {timezone.now().strftime('%d.%m.%Y %H:%M')}", styles['Normal']))
    elements.append(Spacer(1, 20))
    
    total_users = CustomUser.objects.count()
    total_teachers = CustomUser.objects.filter(role='teacher').count()
    total_students = CustomUser.objects.filter(role='student').count()
    
    stats_data = [
        ['Ko\'rsatkich', 'Soni'],
        ['Jami foydalanuvchilar', str(total_users)],
        ['O\'quvchilar', str(total_students)],
        ['O\'qituvchilar', str(total_teachers)],
    ]
    
    table = Table(stats_data, colWidths=[250, 150])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0d6efd')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#dee2e6')),
        ('PADDING', (0, 0), (-1, -1), 8),
    ]))
    elements.append(table)
    
    doc.build(elements)
    buffer.seek(0)
    
    response = HttpResponse(buffer, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="LMS_Stats_{timezone.now().strftime("%Y%m%d")}.pdf"'
    return response


# Section management views
@login_required
def add_section(request, pk):
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')
    
    profession = get_object_or_404(Profession, pk=pk)
    
    if request.method == 'POST':
        form = SectionForm(request.POST)
        if form.is_valid():
            section = form.save(commit=False)
            section.profession = profession
            section.save()
            messages.success(request, "Bo'lim muvaffaqiyatli qo'shildi!")
            return redirect('manage_lessons', pk=pk)
    else:
        form = SectionForm()
    
    return render(request, 'accounts/manage/add_section.html', {
        'profession': profession,
        'form': form,
    })


@login_required
def edit_section(request, pk):
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')
    
    section = get_object_or_404(Section, pk=pk)
    
    if request.method == 'POST':
        form = SectionForm(request.POST, instance=section)
        if form.is_valid():
            form.save()
            messages.success(request, "Bo'lim muvaffaqiyatli yangilandi!")
            return redirect('manage_lessons', pk=section.profession.pk)
    else:
        form = SectionForm(instance=section)
    
    return render(request, 'accounts/manage/edit_section.html', {
        'section': section,
        'form': form,
    })


@login_required
def delete_section(request, pk):
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')
    
    section = get_object_or_404(Section, pk=pk)
    profession_pk = section.profession.pk
    
    if request.method == 'POST':
        section.delete()
        messages.success(request, "Bo'lim o'chirildi!")
        return redirect('manage_lessons', pk=profession_pk)
    
    return render(request, 'accounts/manage/delete_section.html', {'section': section})


# ============== ADMIN: SECTIONS ==============

@login_required
def admin_sections(request):
    if not request.user.is_admin:
        return redirect('home')
    
    sections = Section.objects.all().select_related('profession').prefetch_related('lessons')
    
    profession_id = request.GET.get('profession')
    if profession_id:
        sections = sections.filter(profession_id=profession_id)
    
    q = request.GET.get('q')
    if q:
        sections = sections.filter(title__icontains=q)
    
    professions = Profession.objects.all()
    
    return render(request, 'accounts/admin/sections.html', {
        'sections': sections,
        'professions': professions,
    })


@login_required
def admin_section_add(request):
    if not request.user.is_admin:
        return redirect('home')
    
    if request.method == 'POST':
        profession_id = request.POST.get('profession')
        title = request.POST.get('title')
        description = request.POST.get('description', '')
        order = request.POST.get('order', 0)
        is_active = 'is_active' in request.POST
        
        if profession_id and title:
            Section.objects.create(
                profession_id=profession_id,
                title=title,
                description=description,
                order=int(order),
                is_active=is_active
            )
            messages.success(request, "Bo'lim qo'shildi!")
            return redirect('admin_sections')
    
    professions = Profession.objects.all()
    return render(request, 'accounts/admin/section_form.html', {'professions': professions})


@login_required
def admin_section_edit(request, pk):
    if not request.user.is_admin:
        return redirect('home')
    
    section = get_object_or_404(Section, pk=pk)
    
    if request.method == 'POST':
        section.profession_id = request.POST.get('profession')
        section.title = request.POST.get('title')
        section.description = request.POST.get('description', '')
        section.order = int(request.POST.get('order', 0))
        section.is_active = 'is_active' in request.POST
        section.save()
        messages.success(request, "Bo'lim yangilandi!")
        return redirect('admin_sections')
    
    professions = Profession.objects.all()
    return render(request, 'accounts/admin/section_form.html', {
        'section': section,
        'professions': professions,
    })


@login_required
def admin_section_delete(request, pk):
    if not request.user.is_admin:
        return redirect('home')
    
    section = get_object_or_404(Section, pk=pk)
    
    if request.method == 'POST':
        section.delete()
        messages.success(request, "Bo'lim o'chirildi!")
        return redirect('admin_sections')
    
    return render(request, 'accounts/admin/section_delete.html', {'section': section})


# ==================== COIN BOSHQARUVI ====================

@login_required
def admin_manage_coins(request):
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kirish huquqi yo'q!")
        return redirect('home')

    users = CustomUser.objects.filter(role='student').order_by('-coins', 'first_name')

    if request.method == 'POST':
        user_id = request.POST.get('user_id')
        amount = request.POST.get('amount')
        action = request.POST.get('action')
        reason = request.POST.get('reason', '').strip()

        try:
            amount = int(amount)
            user = CustomUser.objects.get(pk=user_id)

            if action == 'add':
                user.coins += amount
                user.save()

                text = f"Sizga {amount} coin rag'batlantirish sifatida berildi! 🎉"
                if reason:
                    text += f"\n\nSabab: {reason}"

                Message.objects.create(
                    sender=request.user,
                    recipient=user,
                    title="Coin rag'batlantirish",   # ✅ subject emas, title
                    content=text,
                    message_type='system'
                )

                messages.success(request, f"{user.full_name}ga {amount} coin berildi!")

            elif action == 'remove':
                if user.coins >= amount:
                    user.coins -= amount
                    user.save()

                    text = f"Sizdan {amount} coin ayirildi."
                    if reason:
                        text += f"\n\nSabab: {reason}"

                    Message.objects.create(
                        sender=request.user,
                        recipient=user,
                        title="Coin ayirildi",        # ✅ title
                        content=text,
                        message_type='system'
                    )

                    messages.success(request, f"{user.full_name}dan {amount} coin ayirildi!")
                else:
                    messages.error(request, f"{user.full_name}da yetarli coin yo'q! (Mavjud: {user.coins})")

        except (ValueError, CustomUser.DoesNotExist):
            messages.error(request, "Xatolik yuz berdi!")

        return redirect('admin_manage_coins')

    return render(request, 'accounts/admin/manage_coins.html', {
        'users': users,
    })


# ==================== YORDAM SO'ROVLARI ====================

@login_required
def submit_help_request(request):
    if request.method == 'POST':
        subject = request.POST.get('subject', '').strip()
        message = request.POST.get('message', '').strip()
        image = request.FILES.get('image')
        
        if not subject or not message:
            return JsonResponse({'success': False, 'error': "Mavzu va xabar to'ldirilishi shart!"})
        
        HelpRequest.objects.create(
            user=request.user,
            subject=subject,
            message=message,
            image=image
        )
        
        return JsonResponse({'success': True, 'message': "Xabaringiz yuborildi! Tez orada javob beramiz."})
    
    return JsonResponse({'success': False, 'error': 'Noto\'g\'ri so\'rov'})


@login_required
def admin_help_requests(request):
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kirish huquqi yo'q!")
        return redirect('home')
    
    status_filter = request.GET.get('status', '')
    requests_list = HelpRequest.objects.select_related('user').all()
    
    if status_filter:
        requests_list = requests_list.filter(status=status_filter)
    
    context = {
        'requests': requests_list,
        'status_filter': status_filter,
        'pending_count': HelpRequest.objects.filter(status='pending').count(),
    }
    return render(request, 'accounts/admin/help_requests.html', context)


@login_required
def admin_help_request_detail(request, pk):
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kirish huquqi yo'q!")
        return redirect('home')
    
    help_request = get_object_or_404(HelpRequest, pk=pk)
    
    if request.method == 'POST':
        status = request.POST.get('status')
        admin_response = request.POST.get('admin_response', '').strip()
        
        if status:
            help_request.status = status
        if admin_response:
            help_request.admin_response = admin_response
        help_request.save()
        
        messages.success(request, "So'rov yangilandi!")
        return redirect('admin_help_request_detail', pk=pk)
    
    return render(request, 'accounts/admin/help_request_detail.html', {'help_request': help_request})


# ==================== TO'LOV ESLATMASI ====================

@login_required
def send_payment_reminder(request, pk):
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kirish huquqi yo'q!")
        return redirect('home')
    
    student = get_object_or_404(CustomUser, pk=pk, role='student')
    
    Message.objects.create(
        sender=request.user,
        recipient=student,
        title="To'lov eslatmasi",
        content=f"Hurmatli {student.full_name},\n\nSizning oylik to'lovingiz muddati yaqinlashmoqda. Iltimos, o'z vaqtida to'lov qilishni unutmang.\n\nHurmat bilan,\nIT Creative jamoasi",
        message_type='payment'
    )
    
    messages.success(request, f"{student.full_name}ga to'lov eslatmasi yuborildi!")
    return redirect('admin_payments')


@login_required
def send_bulk_payment_reminders(request):
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kirish huquqi yo'q!")
        return redirect('home')
    
    if request.method == 'POST':
        unpaid_students = CustomUser.objects.filter(role='student').exclude(
            payment_status__is_paid=True
        )
        
        count = 0
        for student in unpaid_students:
            Message.objects.create(
                sender=request.user,
                recipient=student,
                title="To'lov eslatmasi",
                content=f"Hurmatli {student.full_name},\n\nSizning oylik to'lovingiz muddati yaqinlashmoqda. Iltimos, o'z vaqtida to'lov qilishni unutmang.\n\nHurmat bilan,\nIT Creative jamoasi",
                message_type='payment'
            )
            count += 1
        
        messages.success(request, f"{count} ta o'quvchiga to'lov eslatmasi yuborildi!")
    
    return redirect('admin_payments')


# ==================== CHEGIRMALAR (SKIDKALAR) ====================

@login_required
def admin_discounts(request):
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kirish huquqi yo'q!")
        return redirect('home')
    
    discounts = Discount.objects.select_related('profession').all()
    
    profession_id = request.GET.get('profession')
    if profession_id:
        discounts = discounts.filter(profession_id=profession_id)
    
    status_filter = request.GET.get('status')
    if status_filter == 'active':
        discounts = discounts.filter(is_active=True)
    elif status_filter == 'inactive':
        discounts = discounts.filter(is_active=False)
    
    q = request.GET.get('q')
    if q:
        discounts = discounts.filter(name__icontains=q)
    
    professions = Profession.objects.all()
    
    context = {
        'discounts': discounts,
        'professions': professions,
        'total_discounts': Discount.objects.count(),
        'active_discounts': Discount.objects.filter(is_active=True).count(),
        'inactive_discounts': Discount.objects.filter(is_active=False).count(),
    }
    return render(request, 'accounts/admin/discounts.html', context)


@login_required
def admin_discount_add(request):
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kirish huquqi yo'q!")
        return redirect('home')
    
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()
        discount_type = request.POST.get('discount_type', 'percentage')
        discount_value = request.POST.get('discount_value', 0)
        min_coins_required = request.POST.get('min_coins_required', 0)
        profession_id = request.POST.get('profession')
        is_active = 'is_active' in request.POST
        valid_from = request.POST.get('valid_from') or None
        valid_until = request.POST.get('valid_until') or None
        
        if name and discount_value:
            Discount.objects.create(
                name=name,
                description=description,
                discount_type=discount_type,
                discount_value=int(discount_value),
                min_coins_required=int(min_coins_required) if min_coins_required else 0,
                profession_id=profession_id if profession_id else None,
                is_active=is_active,
                valid_from=valid_from,
                valid_until=valid_until
            )
            messages.success(request, "Chegirma muvaffaqiyatli qo'shildi!")
            return redirect('admin_discounts')
        else:
            messages.error(request, "Chegirma nomi va qiymati kiritilishi shart!")
    
    professions = Profession.objects.all()
    return render(request, 'accounts/admin/discount_form.html', {'professions': professions})


@login_required
def admin_discount_edit(request, pk):
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kirish huquqi yo'q!")
        return redirect('home')
    
    discount = get_object_or_404(Discount, pk=pk)
    
    if request.method == 'POST':
        discount.name = request.POST.get('name', '').strip()
        discount.description = request.POST.get('description', '').strip()
        discount.discount_type = request.POST.get('discount_type', 'percentage')
        discount.discount_value = int(request.POST.get('discount_value', 0))
        discount.min_coins_required = int(request.POST.get('min_coins_required', 0) or 0)
        profession_id = request.POST.get('profession')
        discount.profession_id = profession_id if profession_id else None
        discount.is_active = 'is_active' in request.POST
        valid_from = request.POST.get('valid_from')
        valid_until = request.POST.get('valid_until')
        discount.valid_from = valid_from if valid_from else None
        discount.valid_until = valid_until if valid_until else None
        discount.save()
        
        messages.success(request, "Chegirma muvaffaqiyatli yangilandi!")
        return redirect('admin_discounts')
    
    professions = Profession.objects.all()
    return render(request, 'accounts/admin/discount_form.html', {
        'discount': discount,
        'professions': professions,
    })


@login_required
def admin_discount_delete(request, pk):
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kirish huquqi yo'q!")
        return redirect('home')
    
    discount = get_object_or_404(Discount, pk=pk)
    
    if request.method == 'POST':
        discount.delete()
        messages.success(request, "Chegirma o'chirildi!")
        return redirect('admin_discounts')
    
    return render(request, 'accounts/admin/discount_delete.html', {'discount': discount})


# ==================== CODING / HTML DEPLOY ====================

@login_required
def my_deploys(request):
    """O'quvchining deploy qilgan sahifalari"""
    deploys = HTMLDeploy.objects.filter(user=request.user)
    return render(request, 'accounts/coding/my_deploys.html', {'deploys': deploys})


@login_required
def create_deploy(request):
    """Yangi HTML deploy qilish"""
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        file_name = request.POST.get('file_name', '').strip()
        description = request.POST.get('description', '').strip()
        profession_id = request.POST.get('profession')
        
        # Fayl yuklash yoki matn kiritish
        html_file = request.FILES.get('html_file')
        html_content = request.POST.get('html_content', '').strip()
        
        if html_file:
            html_content = html_file.read().decode('utf-8', errors='ignore')
        
        if not title or not file_name or not html_content:
            messages.error(request, "Barcha maydonlarni to'ldiring!")
            return redirect('create_deploy')
        
        # Fayl nomini tozalash
        file_name = file_name.lower().replace(' ', '_')
        if not file_name.endswith('.html'):
            file_name += '.html'
        
        # Mavjudligini tekshirish
        if HTMLDeploy.objects.filter(user=request.user, file_name=file_name).exists():
            messages.error(request, f"'{file_name}' nomli fayl allaqachon mavjud!")
            return redirect('create_deploy')
        
        deploy = HTMLDeploy.objects.create(
            user=request.user,
            profession_id=profession_id if profession_id else None,
            title=title,
            file_name=file_name,
            html_content=html_content,
            description=description,
        )
        
        messages.success(request, f"Sahifa muvaffaqiyatli deploy qilindi! URL: {deploy.get_url()}")
        return redirect('my_deploys')
    
    professions = Profession.objects.all()
    return render(request, 'accounts/coding/create_deploy.html', {'professions': professions})


@login_required
def edit_deploy(request, pk):
    """Deploy qilingan sahifani tahrirlash"""
    deploy = get_object_or_404(HTMLDeploy, pk=pk, user=request.user)
    
    if request.method == 'POST':
        deploy.title = request.POST.get('title', '').strip()
        deploy.description = request.POST.get('description', '').strip()
        profession_id = request.POST.get('profession')
        deploy.profession_id = profession_id if profession_id else None
        
        # Fayl yuklash yoki matn kiritish
        html_file = request.FILES.get('html_file')
        html_content = request.POST.get('html_content', '').strip()
        
        if html_file:
            deploy.html_content = html_file.read().decode('utf-8', errors='ignore')
        elif html_content:
            deploy.html_content = html_content
        
        deploy.is_active = 'is_active' in request.POST
        deploy.save()
        
        messages.success(request, "Sahifa yangilandi!")
        return redirect('my_deploys')
    
    professions = Profession.objects.all()
    return render(request, 'accounts/coding/edit_deploy.html', {
        'deploy': deploy,
        'professions': professions,
    })


@login_required
def delete_deploy(request, pk):
    """Deploy qilingan sahifani o'chirish"""
    deploy = get_object_or_404(HTMLDeploy, pk=pk, user=request.user)
    
    if request.method == 'POST':
        deploy.delete()
        messages.success(request, "Sahifa o'chirildi!")
        return redirect('my_deploys')
    
    return render(request, 'accounts/coding/delete_deploy.html', {'deploy': deploy})


def view_deployed_page(request, username, filename):
    """Deploy qilingan sahifani ko'rsatish (login talab qilinmaydi)"""
    user = get_object_or_404(CustomUser, username=username)
    deploy = get_object_or_404(HTMLDeploy, user=user, file_name=filename, is_active=True)
    
    # Ko'rishlar sonini oshirish
    deploy.views_count += 1
    deploy.save(update_fields=['views_count'])
    
    return HttpResponse(deploy.html_content, content_type='text/html')


# ==================== ADMIN: HTML DEPLOYS ====================

@login_required
def admin_deploys(request):
    """Admin: Barcha deploylar"""
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kirish huquqi yo'q!")
        return redirect('home')
    
    deploys = HTMLDeploy.objects.select_related('user', 'profession').all()
    
    # Filterlar
    user_id = request.GET.get('user')
    if user_id:
        deploys = deploys.filter(user_id=user_id)
    
    status = request.GET.get('status')
    if status == 'active':
        deploys = deploys.filter(is_active=True)
    elif status == 'inactive':
        deploys = deploys.filter(is_active=False)
    
    q = request.GET.get('q')
    if q:
        deploys = deploys.filter(title__icontains=q)
    
    users = CustomUser.objects.filter(deploys__isnull=False).distinct()
    
    context = {
        'deploys': deploys,
        'users': users,
        'total_deploys': HTMLDeploy.objects.count(),
        'active_deploys': HTMLDeploy.objects.filter(is_active=True).count(),
        'total_views': HTMLDeploy.objects.aggregate(total=Sum('views_count'))['total'] or 0,
    }
    return render(request, 'accounts/admin/deploys.html', context)


@login_required
def admin_deploy_toggle(request, pk):
    """Admin: Deploy faolligini o'zgartirish"""
    if not request.user.is_admin:
        return redirect('home')
    
    deploy = get_object_or_404(HTMLDeploy, pk=pk)
    deploy.is_active = not deploy.is_active
    deploy.save()
    
    status = "faollashtirildi" if deploy.is_active else "o'chirildi"
    messages.success(request, f"'{deploy.title}' {status}!")
    return redirect('admin_deploys')


@login_required
def admin_deploy_delete(request, pk):
    """Admin: Deployni o'chirish"""
    if not request.user.is_admin:
        return redirect('home')
    
    deploy = get_object_or_404(HTMLDeploy, pk=pk)
    
    if request.method == 'POST':
        deploy.delete()
        messages.success(request, "Deploy o'chirildi!")
        return redirect('admin_deploys')
    
    return render(request, 'accounts/admin/deploy_delete.html', {'deploy': deploy})


# ==================== ADMIN: DARSLAR STATISTIKASI ====================

@login_required
def admin_lesson_statistics(request):
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kirish huquqi yo'q!")
        return redirect('home')
    
    profession_id = request.GET.get('profession')
    
    # Barcha darslar
    lessons = Lesson.objects.select_related('profession', 'section').all()
    if profession_id:
        lessons = lessons.filter(profession_id=profession_id)
    
    # Video darslar statistikasi - ko'rilmagan
    video_lessons = lessons.filter(lesson_type='video')
    video_stats = []
    for lesson in video_lessons:
        try:
            video = lesson.video
            total_enrolled = CourseEnrollment.objects.filter(profession=lesson.profession).count()
            watched_count = VideoProgress.objects.filter(video=video, watched=True).count()
            not_watched = total_enrolled - watched_count
            if total_enrolled > 0:
                completion_rate = (watched_count / total_enrolled) * 100
            else:
                completion_rate = 0
            
            # Kim ko'rmagan
            watched_user_ids = VideoProgress.objects.filter(video=video, watched=True).values_list('user_id', flat=True)
            not_watched_users = CustomUser.objects.filter(
                enrollments__profession=lesson.profession
            ).exclude(pk__in=watched_user_ids)[:5]
            
            video_stats.append({
                'lesson': lesson,
                'total_enrolled': total_enrolled,
                'watched': watched_count,
                'not_watched': not_watched,
                'completion_rate': round(completion_rate, 1),
                'not_watched_users': not_watched_users,
            })
        except:
            pass
    
    # Eng ko'p tashlab ketilgan videolar
    video_stats_sorted = sorted(video_stats, key=lambda x: x['not_watched'], reverse=True)[:10]
    
    # Test statistikasi - yechilmagan
    test_lessons = lessons.filter(lesson_type='test')
    test_stats = []
    for lesson in test_lessons:
        try:
            test = lesson.test
            total_enrolled = CourseEnrollment.objects.filter(profession=lesson.profession).count()
            completed_count = TestResult.objects.filter(test=test).values('student').distinct().count()
            not_completed = total_enrolled - completed_count
            if total_enrolled > 0:
                completion_rate = (completed_count / total_enrolled) * 100
            else:
                completion_rate = 0
            
            # O'rtacha ball
            avg_score = TestResult.objects.filter(test=test).aggregate(avg=Avg('score'))['avg'] or 0
            
            # Kim yechmagan
            completed_user_ids = TestResult.objects.filter(test=test).values_list('student_id', flat=True)
            not_completed_users = CustomUser.objects.filter(
                enrollments__profession=lesson.profession
            ).exclude(pk__in=completed_user_ids)[:5]
            
            test_stats.append({
                'lesson': lesson,
                'total_enrolled': total_enrolled,
                'completed': completed_count,
                'not_completed': not_completed,
                'completion_rate': round(completion_rate, 1),
                'avg_score': round(avg_score, 1),
                'not_completed_users': not_completed_users,
            })
        except:
            pass
    
    # Eng ko'p tashlab ketilgan testlar
    test_stats_sorted = sorted(test_stats, key=lambda x: x['not_completed'], reverse=True)[:10]
    
    # Uyga vazifa statistikasi - topshirilmagan
    homework_lessons = lessons.filter(lesson_type='homework')
    homework_stats = []
    for lesson in homework_lessons:
        try:
            homework = lesson.homework
            total_enrolled = CourseEnrollment.objects.filter(profession=lesson.profession).count()
            submitted_count = HomeworkSubmission.objects.filter(homework=homework).values('student').distinct().count()
            not_submitted = total_enrolled - submitted_count
            if total_enrolled > 0:
                completion_rate = (submitted_count / total_enrolled) * 100
            else:
                completion_rate = 0
            
            # Kim topshirmagan
            submitted_user_ids = HomeworkSubmission.objects.filter(homework=homework).values_list('student_id', flat=True)
            not_submitted_users = CustomUser.objects.filter(
                enrollments__profession=lesson.profession
            ).exclude(pk__in=submitted_user_ids)[:5]
            
            homework_stats.append({
                'lesson': lesson,
                'total_enrolled': total_enrolled,
                'submitted': submitted_count,
                'not_submitted': not_submitted,
                'completion_rate': round(completion_rate, 1),
                'not_submitted_users': not_submitted_users,
            })
        except:
            pass
    
    # Eng ko'p tashlab ketilgan vazifalar
    homework_stats_sorted = sorted(homework_stats, key=lambda x: x['not_submitted'], reverse=True)[:10]
    
    # Passiv o'quvchilar - oxirgi 7 kunda faollik ko'rsatmaganlar
    week_ago = timezone.now() - timedelta(days=7)
    passive_students = CustomUser.objects.filter(
        role='student',
        is_blocked=False
    ).filter(
        Q(last_activity__lt=week_ago) | Q(last_activity__isnull=True)
    ).order_by('last_activity')[:20]
    
    # Qiynalayotgan o'quvchilar - testlardan past ball olganlar
    struggling_students = CustomUser.objects.filter(
        role='student',
        test_results__score__lt=50
    ).annotate(
        low_score_count=Count('test_results', filter=Q(test_results__score__lt=50)),
        avg_score=Avg('test_results__score')
    ).filter(low_score_count__gte=2).order_by('-low_score_count')[:15]
    
    professions = Profession.objects.all()
    
    context = {
        'video_stats': video_stats_sorted,
        'test_stats': test_stats_sorted,
        'homework_stats': homework_stats_sorted,
        'passive_students': passive_students,
        'struggling_students': struggling_students,
        'professions': professions,
        'selected_profession': profession_id,
    }
    
    return render(request, 'accounts/admin/lesson_statistics.html', context)


# ==================== QURILMALAR BOSHQARUVI ====================

@login_required
def my_devices(request):
    devices = request.user.devices.filter(is_active=True).order_by('-last_login')
    current_session_key = request.session.session_key
    
    # Find current device
    current_device = None
    for device in devices:
        active_session = device.sessions.filter(session_key=current_session_key, is_active=True).first()
        if active_session:
            current_device = device
            break
    
    return render(request, 'accounts/devices.html', {
        'devices': devices,
        'current_device': current_device,
    })


@login_required
def remove_device(request, pk):
    device = get_object_or_404(UserDevice, pk=pk, user=request.user)
    current_session_key = request.session.session_key
    
    # Check if it's the current device
    is_current = device.sessions.filter(session_key=current_session_key, is_active=True).exists()
    
    if is_current:
        messages.error(request, "Joriy qurilmani o'chirib bo'lmaydi!")
        return redirect('my_devices')
    
    # Deactivate all sessions for this device
    device.sessions.update(is_active=False)
    device.is_active = False
    device.save()
    
    messages.success(request, f"'{device.device_name}' qurilmasi o'chirildi!")
    return redirect('my_devices')


@login_required
def logout_device(request, pk):
    device = get_object_or_404(UserDevice, pk=pk, user=request.user)
    current_session_key = request.session.session_key
    
    # Check if it's the current device
    is_current = device.sessions.filter(session_key=current_session_key, is_active=True).exists()
    
    if is_current:
        messages.error(request, "Joriy qurilmadan chiqish uchun 'Chiqish' tugmasini ishlating!")
        return redirect('my_devices')
    
    # Deactivate all sessions for this device
    device.sessions.filter(is_active=True).update(is_active=False)
    
    messages.success(request, f"'{device.device_name}' qurilmasidan chiqildi!")
    return redirect('my_devices')


@login_required
def trust_device(request, pk):
    device = get_object_or_404(UserDevice, pk=pk, user=request.user)
    device.is_trusted = not device.is_trusted
    device.save()
    
    if device.is_trusted:
        messages.success(request, f"'{device.device_name}' ishonchli qurilma sifatida belgilandi!")
    else:
        messages.success(request, f"'{device.device_name}' ishonchli qurilmalar ro'yxatidan chiqarildi!")
    
    return redirect('my_devices')


@login_required
def logout_all_devices(request):
    if request.method == 'POST':
        current_session_key = request.session.session_key
        
        # Deactivate all sessions except current
        UserSession.objects.filter(
            user=request.user,
            is_active=True
        ).exclude(session_key=current_session_key).update(is_active=False)
        
        messages.success(request, "Barcha boshqa qurilmalardan chiqildi!")
    
    return redirect('my_devices')


# ==================== ADMIN: QURILMALAR ====================

@login_required
def admin_user_devices(request, pk):
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kirish huquqi yo'q!")
        return redirect('home')
    
    user = get_object_or_404(CustomUser, pk=pk)
    devices = user.devices.filter(is_active=True).order_by('-last_login')
    active_sessions = UserSession.objects.filter(user=user, is_active=True)
    
    return render(request, 'accounts/admin/user_devices.html', {
        'target_user': user,
        'devices': devices,
        'active_sessions': active_sessions,
    })


@login_required
def admin_logout_user_device(request, user_pk, device_pk):
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kirish huquqi yo'q!")
        return redirect('home')
    
    device = get_object_or_404(UserDevice, pk=device_pk, user_id=user_pk)
    device.sessions.filter(is_active=True).update(is_active=False)
    
    messages.success(request, f"'{device.device_name}' qurilmasidan chiqildi!")
    return redirect('admin_user_devices', pk=user_pk)


@login_required
def admin_logout_all_user_devices(request, pk):
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kirish huquqi yo'q!")
        return redirect('home')
    
    if request.method == 'POST':
        user = get_object_or_404(CustomUser, pk=pk)
        UserSession.objects.filter(user=user, is_active=True).update(is_active=False)
        messages.success(request, f"{user.full_name}ning barcha qurilmalaridan chiqildi!")
    
    return redirect('admin_user_devices', pk=pk)


