from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from .desktop_auth import POST_LOGIN_SESSION_KEY, post_login_target, peek_post_login_target
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_GET, require_POST
from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.conf import settings
from django.db.models import Count, F, Max, Q, Sum, Avg
from django.db import models, transaction
from django.utils import timezone
from datetime import timedelta, date, datetime
import json
import math
import random
import secrets
import time
from .alphabet import to_new_alphabet
from .forms import (
    RegisterForm, LoginForm, ProfessionForm, UserProfileForm, 
    AdminUserEditForm, ChangePasswordForm, VideoLessonForm, HomeworkForm, DictionaryForm,
    HomeworkSubmissionForm, HomeworkGradeForm, TestForm, TestQuestionForm, CertificateForm,
    SectionForm, PasswordResetRequestForm
)
from .models import (
    Profession, Section, CustomUser, CourseEnrollment, CourseEnrollmentRequest, CourseUnenrollRequest, Lesson, VideoLesson, VideoProgress,
    Homework, HomeworkSubmission, Test, TestQuestion, TestAnswer, TestResult,
    TestUserAnswer, Dictionary, DictionaryWord, Certificate, Message, PaymentStatus, PaymentRecord, HelpRequest, Discount,
    UserDevice, UserSession, SystemReport, PasswordResetRequest, LoginLocation,
    Parent, ParentMessage, PremiumRequest, BlockedSection, MaintenanceMode,
    CourseApplication, SpeakingTask, SpeakingSubmission, LoginAttempt, DailySectionAccess,
    DailyLessonAccess, FinalTestPayment, StudentLessonToken, StudentCourseToken,
    SurveyForm, SurveyQuestion, SurveyResponse, SurveyAnswer, StudyGroup, StudentAttendance,
    final_test_prerequisites_met, final_lesson_q, daily_limit_active,
)
from django.contrib.auth.hashers import make_password
from .security import (
    get_client_ip, check_rate_limit, is_rate_limited, validate_parent_password,
    safe_error,
)
from coin.models import ActivityLog, CoinTransaction
from blog.models import Post
from quiz.models import Attempt, Quiz, QuizSettings
from ai_yordamchi import jobs as ai_jobs
from ai_yordamchi.jobs import ai_offload

from django.core.management import call_command
from django.core.cache import cache
from .public_cache import landing_data, get_performance_cache
from django.core.paginator import Paginator
from telegram_bot.utils import notify_admins, notify_user, notify_group, notify_application_recipients


def _to_int(value, default=0):
    """Xavfsiz int ögiriş: bösh/notöğri qiymatda default qaytaradi.
    Filtr/qidiruv formasi böş month/year yuborganda 500 xatolikni oldini oladi."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _redirect_student_to_token(request, lesson):
    """Öquvçilarni PK URL'dan bloklaş — faqat token URL orqali kiriş mumkin."""
    if request.user.is_authenticated and request.user.is_student and not request.user.is_admin:
        return render(request, 'accounts/student_token_required.html', status=403)
    return None


def _reset_course_progress(user, profession):
    """Öquvçi kursdan çiqib qayta yozilganda barça progressni tozalaş.
    Video progress, homework submissions, test results, speaking submissions va sertifikatlarni öçiradi."""
    # Video progress
    VideoProgress.objects.filter(
        user=user,
        video__lesson__profession=profession
    ).delete()

    # Homework submissions (va ularga boğliq messages, revisions cascade bilan öçadi)
    HomeworkSubmission.objects.filter(
        student=user,
        homework__lesson__profession=profession
    ).delete()

    # Test results (va ularga boğliq user_answers cascade bilan öçadi)
    TestResult.objects.filter(
        student=user,
        test__lesson__profession=profession
    ).delete()

    # Speaking submissions
    SpeakingSubmission.objects.filter(
        student=user,
        speaking_task__lesson__profession=profession
    ).delete()

    # Sertifikatlarni bekor qiliş
    Certificate.objects.filter(
        student=user,
        profession=profession
    ).delete()


def _generate_ai_article(lesson_title, profession_name):
    """Video mavzusiga qarab AI maqola yaratiş (background da işlamaydi, sync)"""
    from django.conf import settings
    api_key = getattr(settings, 'OPENAI_API_KEY', None)
    if not api_key or not api_key.strip():
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key.strip(), timeout=60)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Sen tajribali IT öqituvçisan. Özbek tilida professional maqola yoz. "
                        "HTML formatda yoz (h2, h3, p, ul, li, strong, em, code taglardan foydalan). "
                        "Maqola 400-600 söz bölsin. Mavzuni çuqur tuşuntir, misollar keltir. "
                        "Boşida qisqa kiriş, keyin asosiy tuşunçalar, oxirida xulosa bölsin. "
                        "Faqat HTML content qaytar, <html>, <body>, <head> taglarini IŞLATMA."
                    )
                },
                {
                    "role": "user",
                    "content": f"Kurs: {profession_name}\nMavzu: {lesson_title}\n\nŞu mavzu böyiça öquv maqolasi yoz."
                }
            ],
            max_tokens=2000,
            temperature=0.7,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"AI maqola xatolik: {e}")
        return None


@ai_offload
def generate_ai_homework(request):
    """Bölim nomiga qarab AI orqali uyga vazifa yaratiş"""
    if not (request.user.is_authenticated and (request.user.is_admin or request.user.is_teacher)):
        return JsonResponse({'error': 'Ruxsat yöq'}, status=403)

    if request.user.is_teacher and not getattr(request.user, 'ai_access_enabled', True):
        return JsonResponse({
            'error': "AI funksiyasi sizga admin tomonidan bloklangan. "
                     "Bu funksiyadan foydalaniş uçun admin bilan boğlaning.",
            'ai_blocked': True,
        }, status=403)
    
    if request.method != 'POST':
        return JsonResponse({'error': 'POST talab qilinadi'}, status=405)
    
    import json
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        data = request.POST
    
    section_id = data.get('section_id')
    profession_id = data.get('profession_id')
    
    section_name = ''
    profession_name = ''
    
    if section_id:
        try:
            section = Section.objects.get(pk=section_id)
            section_name = section.title
            profession_name = section.profession.name
        except Section.DoesNotExist:
            pass
    
    if not profession_name and profession_id:
        try:
            profession = Profession.objects.get(pk=profession_id)
            profession_name = profession.name
        except Profession.DoesNotExist:
            pass
    
    if not section_name and not profession_name:
        return JsonResponse({'error': 'Bölim yoki kurs tanlanmagan'}, status=400)
    
    from django.conf import settings
    api_key = getattr(settings, 'OPENAI_API_KEY', None)
    if not api_key or not api_key.strip():
        return JsonResponse({'error': 'AI xizmati sozlanmagan'}, status=500)
    
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key.strip(), timeout=60)
        
        topic = section_name if section_name else profession_name
        prompt_context = f"Kurs: {profession_name}" if profession_name else ""
        if section_name:
            prompt_context += f"\nBölim: {section_name}"
        
        example_homework = (
            '<h3>📌 Vazifa maqsadi</h3>'
            '<p>Bu vazifada HTML asosiy teglarini örganasiz. Sarlavhalar, röyxatlar va havolalar yaratişni maşq qilasiz.</p>'
            '<h3>📝 Topşiriqlar</h3>'
            '<p><strong>1-topshiriq: Şaxsiy sahifa yarating.</strong> Özingiz haqingizda HTML sahifa yarating. '
            'Sahifada h1 teg bilan ismingiz, h2 bilan "Men haqimda" sarlavhasi va p tegda 3-4 jumla yozing. '
            'Misol: <code>&lt;h1&gt;Ali Valiyev&lt;/h1&gt;</code></p>'
            '<p><strong>2-topshiriq: Jadval tuzing.</strong> 4 qatorli va 3 ustunli jadval yarating. '
            'Jadvalda özingiz va oila a\'zolaringiz haqida ma\'lumot kiriting (ism, yoş, kasb). '
            'table, tr, th, td teglarini işlating.</p>'
            '<p><strong>3-topshiriq: Röyxat yarating.</strong> Sevimli filmlaringiz tartibli röyxatini (ol) va '
            'sevimli ovqatlaringiz tartivsiz röyxatini (ul) yarating. Har bir röyxatda kamida 5 ta element bölsin.</p>'
            '<p><strong>4-topshiriq: Havola va rasm qöşing.</strong> Sahifangizga 2 ta havola (a teg) va '
            '1 ta rasm (img teg) qöşing. Rasmga alt atribut yozing.</p>'
            '<h3>✅ Baholaş mezonlari</h3>'
            '<p><strong>Töğri bajarilişi (4 ball):</strong> Barça teglar töğri yopilgan, sahifa brauzerde xatosiz oçiladi.</p>'
            '<p><strong>Kod sifati (3 ball):</strong> Kod tartibli yozilgan, indentatsiya töğri, teglar bir xil stilida.</p>'
            '<p><strong>Ijodkorlik (3 ball):</strong> Qöşimça teglar işlatilgan, sahifa mazmunli va çiroyli.</p>'
            '<p><strong>Umumiy ball: 10</strong></p>'
        )

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Sen tajribali IT öqituvçisan. Senden uyga vazifa yoziş söraladi. "
                        "Özbek tilida yoz. Faqat HTML formatda javob qaytar.\n\n"
                        "MUHIM QOIDALAR:\n"
                        "- Faqat <h3>, <p>, <strong>, <em>, <code> teglarini işlat\n"
                        "- <ol>, <ul>, <li> teglarini IŞLATMA! Örniga <p> içida yoz\n"
                        "- <html>, <body>, <head> teglarini IŞLATMA\n"
                        "- Har bir topşiriqni alohida <p> tegda yoz\n"
                        "- Har bir <p> içida kamida 2-3 jumla bölsin\n"
                        "- Topşiriqlarda ANIQ kod misollari keltir\n\n"
                        "Namuna:\n" + example_homework + "\n\n"
                        "Xuddi şu formatda yoz. Topşiriqlar soni 4 ta. Umumiy ball 10."
                    )
                },
                {
                    "role": "user",
                    "content": f"{prompt_context}\n\nŞu mavzu uçun uyga vazifa yoz. "
                               f"Har bir topşiriqni <p> tegda batafsil yoz, aniq körsatma va kod misoli bölsin."
                }
            ],
            max_tokens=2500,
            temperature=0.7,
        )
        
        homework_content = response.choices[0].message.content.strip()
        title = f"{topic} - Uyga vazifa"
        
        return JsonResponse({
            'success': True,
            'title': title,
            'description': homework_content,
        })
    except Exception as e:
        return JsonResponse(
            {'error': safe_error(e, 'ai_generate_homework', "AI xatolik. Qayta urinib köring.")},
            status=500,
        )


@ai_offload
def generate_ai_test(request):
    """AI orqali test savollari yaratiş"""
    if not (request.user.is_authenticated and (request.user.is_admin or request.user.is_teacher)):
        return JsonResponse({'error': 'Ruxsat yöq'}, status=403)

    if request.user.is_teacher and not getattr(request.user, 'ai_access_enabled', True):
        return JsonResponse({
            'error': "AI funksiyasi sizga admin tomonidan bloklangan. "
                     "Bu funksiyadan foydalaniş uçun admin bilan boğlaning.",
            'ai_blocked': True,
        }, status=403)
    
    if request.method != 'POST':
        return JsonResponse({'error': 'POST talab qilinadi'}, status=405)
    
    import json
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        data = request.POST
    
    test_id = data.get('test_id')
    question_count = data.get('question_count', 5)
    
    try:
        question_count = int(question_count)
        if question_count < 1:
            question_count = 1
        elif question_count > 50:
            question_count = 50
    except (ValueError, TypeError):
        question_count = 5
    
    if not test_id:
        return JsonResponse({'error': 'Test tanlanmagan'}, status=400)
    
    try:
        test = Test.objects.select_related('lesson__section', 'lesson__profession').get(pk=test_id)
    except Test.DoesNotExist:
        return JsonResponse({'error': 'Test topilmadi'}, status=404)
    
    lesson_title = test.lesson.title
    profession_name = test.lesson.profession.name
    section_name = test.lesson.section.title if test.lesson.section else ''
    
    topic = section_name if section_name else lesson_title
    
    from django.conf import settings
    api_key = getattr(settings, 'OPENAI_API_KEY', None)
    if not api_key or not api_key.strip():
        return JsonResponse({'error': 'AI xizmati sozlanmagan (OPENAI_API_KEY topilmadi)'}, status=500)
    
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key.strip(), timeout=120)
        
        prompt_context = f"Kurs: {profession_name}"
        if section_name:
            prompt_context += f"\nBölim: {section_name}"
        prompt_context += f"\nDars: {lesson_title}"
        
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Sen tajribali IT öqituvçisan. Senden test savollari yaratiş söraladi. "
                        "Özbek tilida yoz. Faqat JSON formatda javob qaytar, boşqa heç narsa yozma.\n\n"
                        "Har bir savol uçun 3 ta variant (A, B, C) bölsin.\n"
                        "Töğri javob faqat A, B yoki C bölsin.\n"
                        "Savollar real amaliy bölsin, kodli misollar ham bölişi mumkin.\n"
                        "Javoblar bir-biriga yaqin va çalğituvçi bölsin, LEKIN har bir savolda A, B, C variantlari bir-biridan FARQLI bölişi ŞART. "
                        "Heç qaçon ikki yoki uç variant bir xil matnli bölmasin!\n"
                        "Izoh 1-2 gap bilan tuşuntirsin.\n\n"
                        "JSON formati:\n"
                        "[\n"
                        "  {\n"
                        '    "question": "Savol matni",\n'
                        '    "a": "A varianti",\n'
                        '    "b": "B varianti",\n'
                        '    "c": "C varianti",\n'
                        '    "correct": "A",\n'
                        '    "explanation": "Izoh"\n'
                        "  }\n"
                        "]\n\n"
                        "MUHIM: Faqat JSON array qaytar. Markdown yoki boşqa text qöşma."
                    )
                },
                {
                    "role": "user",
                    "content": f"{prompt_context}\n\nŞu mavzu böyiça {question_count} ta test savoli yarat."
                }
            ],
            max_tokens=4000,
            temperature=0.7,
        )
        
        ai_text = response.choices[0].message.content.strip()
        
        # Clean markdown code blocks if present
        if ai_text.startswith('```'):
            lines = ai_text.split('\n')
            # Remove first line (```json) and last line (```)
            lines = [l for l in lines if not l.strip().startswith('```')]
            ai_text = '\n'.join(lines)
        
        questions_data = json.loads(ai_text)
        
        if not isinstance(questions_data, list):
            return JsonResponse({'error': 'AI notöğri format qaytardi'}, status=500)
        
        from django.db import transaction
        created_count = 0
        
        with transaction.atomic():
            existing_count = test.questions.count()
            for i, qd in enumerate(questions_data):
                q_text = qd.get('question', '').strip()
                opt_a = qd.get('a', '').strip()
                opt_b = qd.get('b', '').strip()
                opt_c = qd.get('c', '').strip()
                correct = qd.get('correct', '').strip().upper()
                explanation = qd.get('explanation', '').strip()
                
                if not q_text or not opt_a or not opt_b or not opt_c or correct not in ('A', 'B', 'C'):
                    continue
                
                # Dublikat variantlarni tekşiriş
                options = [opt_a.lower(), opt_b.lower(), opt_c.lower()]
                if len(set(options)) < 3:
                    # Agar variantlar içida bir xili bölsa, bu savolni ötkazib yuboramiz
                    continue
                
                question = TestQuestion.objects.create(
                    test=test,
                    question_text=q_text,
                    explanation=explanation,
                    order=existing_count + i + 1,
                )
                for letter, text in [('A', opt_a), ('B', opt_b), ('C', opt_c)]:
                    TestAnswer.objects.create(
                        question=question,
                        answer_text=text,
                        is_correct=(letter == correct),
                    )
                created_count += 1
        
        return JsonResponse({
            'success': True,
            'created_count': created_count,
            'message': f'{created_count} ta savol muvaffaqiyatli yaratildi!'
        })
    except json.JSONDecodeError:
        return JsonResponse({'error': 'AI javobini öqişda xatolik. Qayta urinib köring.'}, status=500)
    except Exception as e:
        return JsonResponse(
            {'error': safe_error(e, 'ai_generate_questions', "AI xatolik. Qayta urinib köring.")},
            status=500,
        )


@login_required
@ai_offload
def generate_ai_article_view(request, pk):
    """AJAX: AI maqola yaratiş yoki qayta yaratiş"""
    if not (request.user.is_admin or request.user.is_teacher):
        return JsonResponse({'error': 'Ruxsat yöq'}, status=403)

    if request.user.is_teacher and not getattr(request.user, 'ai_access_enabled', True):
        return JsonResponse({
            'error': "AI funksiyasi sizga admin tomonidan bloklangan. "
                     "Bu funksiyadan foydalaniş uçun admin bilan boğlaning.",
            'ai_blocked': True,
        }, status=403)
    
    lesson = get_object_or_404(Lesson, pk=pk)
    
    if request.method == 'POST':
        ai_article = _generate_ai_article(lesson.title, lesson.profession.name)
        if ai_article:
            lesson.ai_article = ai_article
            lesson.save(update_fields=['ai_article'])
            return JsonResponse({'success': True, 'article': ai_article})
        return JsonResponse({'error': 'AI maqola yaratişda xatolik'}, status=500)
    
    return JsonResponse({'error': 'POST kerak'}, status=405)


def send_course_notification(profession, title, content, sender=None):
    """Kursga yozilgan barça öquvçilarga xabar yuboriş (sayt + Telegram)"""
    import threading
    
    enrollments = CourseEnrollment.objects.filter(profession=profession).select_related('user')
    
    teacher_name = ""
    if sender:
        teacher_name = f"{sender.first_name} {sender.last_name}"
    
    telegram_ids = []
    for enrollment in enrollments:
        Message.objects.create(
            sender=sender,
            recipient=enrollment.user,
            title=title,
            content=content,
            message_type='system'
        )
        if enrollment.user.telegram_id:
            telegram_ids.append(enrollment.user.telegram_id)
    
    def _send_telegram():
        for tid in telegram_ids:
            telegram_text = (
                f"📢 <b>{title}</b>\n\n"
                f"{content}\n\n"
            )
            if teacher_name:
                telegram_text += f"👨‍🏫 Öqituvçi: {teacher_name}\n"
            telegram_text += f"🔗 Saytga kiring va köring!"
            notify_user(tid, telegram_text)
    
    threading.Thread(target=_send_telegram, daemon=True).start()


def register_view(request):
    if request.user.is_authenticated:
        return redirect('home')
    
    # Rate limiting — röyxatdan ötiş uçun
    if request.method == 'POST':
        blocked, msg = check_rate_limit(
            request, 'register', ip_limit=3, id_limit=3, window=3600
        )
        if blocked:
            messages.error(request, msg)
            return render(request, 'accounts/register.html', {'form': RegisterForm()})
    
    if request.method == 'POST':
        form = RegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            from .email_verification import send_verification
            if send_verification(user, request):
                messages.info(request, 'Emailingizga tasdiqlaş havolasi yuborildi. Havolani oçmagunça email hisobga boğlanmaydi.')
            else:
                messages.warning(request, 'Email hali boğlanmagan. Tasdiqlaş havolasi yuborilmadi; profil orqali qayta yuboring.')
            login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            # Login'dagi kabi sessiyani röyxatdan ötkazamiz — aks holda
            # DeviceLimitMiddleware faol UserSession topmay, yangi öquvçini
            # profilni töldiriş sahifasiga yetmasdan tizimdan çiqarib yuboradi.
            _register_device_and_session(request, user)
            notify_admins(
                f"👤 <b>Yangi foydalanuvçi röyxatdan ötdi!</b>\n\n"
                f"📛 {user.first_name} {user.last_name}\n"
                f"🆔 @{user.username}\n"
                f"📱 {user.phone}\n"
                f"🎓 {user.get_role_display()}"
            )
            notify_group(
                f"🎉 <b>Safimizga yangi foydalanuvçi qöşildi!</b>\n\n"
                f"👤 {user.first_name} {user.last_name}\n"
                f"🎓 {user.get_role_display()}\n\n"
                f"🚀 Xuş kelibsiz!"
            )
            messages.success(request, "Röyxatdan muvaffaqiyatli ötdingiz! Endi profilingizni töldiring.")
            # Auto-login qilingan — profilni töldiriş (hujjat tasdiği bilan) sahifasiga.
            return redirect('complete_profile')
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{error}")
    else:
        form = RegisterForm()

    return render(request, 'accounts/register.html', {'form': form})


def _login_render(request, form, login_mode):
    """Login sahifasini render qiliş"""
    # Login token — har bir sahifa yuklanganda yangilanadi
    login_token = request.session.get('login_token')
    if not login_token:
        login_token = LoginAttempt.generate_token()
        request.session['login_token'] = login_token
    return render(request, 'accounts/login.html', {
        'form': form,
        'login_mode': login_mode,
        'login_token': login_token,
    })


def _parse_user_agent(ua):
    """User-Agent dan qurilma nomi, turi, brauzer va OS ni aniqlaş"""
    # Qurilma turi
    if 'Mobile' in ua or 'Android' in ua and 'Tablet' not in ua:
        device_type = 'mobile'
    elif 'Tablet' in ua or 'iPad' in ua:
        device_type = 'tablet'
    else:
        device_type = 'desktop'

    # OS
    if 'iPhone' in ua or 'iPad' in ua:
        os_name = 'iOS'
    elif 'Android' in ua:
        os_name = 'Android'
    elif 'Windows' in ua:
        os_name = 'Windows'
    elif 'Macintosh' in ua or 'Mac OS' in ua:
        os_name = 'MacOS'
    elif 'Linux' in ua:
        os_name = 'Linux'
    else:
        os_name = "Noma'lum"

    # Brauzer
    if 'Edg' in ua:
        browser = 'Edge'
    elif 'OPR' in ua or 'Opera' in ua:
        browser = 'Opera'
    elif 'Chrome' in ua:
        browser = 'Chrome'
    elif 'Firefox' in ua:
        browser = 'Firefox'
    elif 'Safari' in ua:
        browser = 'Safari'
    else:
        browser = "Noma'lum"

    device_name = f"{os_name} - {browser}"
    return device_name, device_type, browser, os_name


def _get_device_id(user, ua, ip_address):
    """User-Agent va IP dan qurilma ID yaratiş"""
    import hashlib
    device_id_raw = f"{user.pk}-{ua}-{ip_address}"
    return hashlib.md5(device_id_raw.encode()).hexdigest()


def _register_device_and_session(request, user):
    """Login paytida qurilma va sessiyani röyxatdan ötkaziş.
    
    Qoidalar:
    - Faqat 1 ta qurilma online böladi. Boşqa sessiyalar öçiriladi.
    """
    from django.contrib.sessions.models import Session as DjangoSession
    
    ua = request.META.get('HTTP_USER_AGENT', '')
    ip_address = get_client_ip(request) or ''
    device_name, device_type, browser, os_name = _parse_user_agent(ua)
    device_id = _get_device_id(user, ua, ip_address)

    # Bu qurilma allaqaçon röyxatdan ötganmi?
    device = UserDevice.objects.filter(user=user, device_id=device_id, is_active=True).first()

    if not device:
        # Yangi qurilma yaratiş (qurilma soni çeklanmagan)
        device, created = UserDevice.objects.get_or_create(
            user=user,
            device_id=device_id,
            defaults={
                'device_name': device_name,
                'device_type': device_type,
                'browser': browser,
                'os': os_name,
                'ip_address': ip_address,
                'is_active': True,
            }
        )
        if not created:
            device.is_active = True
            device.save(update_fields=['is_active', 'last_login'])
    else:
        device.ip_address = ip_address
        device.save(update_fields=['ip_address', 'last_login'])

    # Sessiyani yaratiş
    if not request.session.session_key:
        request.session.create()

    # ✅ FAQAT 1 TA ONLINE: barça eski sessiyalarni öçiriş
    old_sessions = UserSession.objects.filter(user=user, is_active=True)
    old_session_keys = list(old_sessions.values_list('session_key', flat=True))
    DjangoSession.objects.filter(session_key__in=old_session_keys).delete()
    old_sessions.update(is_active=False)

    # Yangi sessiya yaratiş
    UserSession.objects.create(
        user=user,
        device=device,
        session_key=request.session.session_key,
        ip_address=ip_address,
        expires_at=timezone.now() + timedelta(seconds=7200),
        is_active=True,
    )

    # DeviceLimitMiddleware faol sessiyani cache'dan tekşiradi.
    # Eski sessiyalar öçirilgandan keyin cache yangilanmasa, keyingi requestda
    # yangi login qilingan foydalanuvçi darhol logout bölib qolişi mumkin.
    cache.set(f'active_session_{user.id}', request.session.session_key, 120)

    return True


def _record_failed_login(ip_address, username='', user_agent='', login_token=''):
    """Muvaffaqiyatsiz login urinişini yoziş"""
    LoginAttempt.objects.create(
        ip_address=ip_address,
        username=username[:150],
        was_successful=False,
        user_agent=user_agent[:500],
        login_token=login_token,
    )


def _record_successful_login(ip_address, username='', user_agent='', login_token=''):
    """Muvaffaqiyatli loginni yoziş va token urinişlarni tozalaş"""
    LoginAttempt.objects.create(
        ip_address=ip_address,
        username=username[:150],
        was_successful=True,
        user_agent=user_agent[:500],
        login_token=login_token,
    )
    LoginAttempt.clear_token_attempts(login_token)


def _add_one_month(d):
    """Sanaga 1 oy qöşadi, kunni saqlab (kerak bölsa oy oxiriga qisqartirib).
    Masalan: 13-iyun -> 13-iyul, 31-yanvar -> 28/29-fevral. dateutil talab qilmaydi.
    """
    import calendar
    year = d.year + (1 if d.month == 12 else 0)
    month = 1 if d.month == 12 else d.month + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day)


def _auto_deduct_and_unblock(user):
    """
    Öquvçi balansidan oylik yoki qisman tölovni yeçib, unblock qilişga urinadi.
    - Balans oylik tölovdan kam bölsa (masalan 400 minglik tölovga 200 ming bölsa),
      proporsional kunlar (15 kun) hisoblanadi va foydalanuvçi blokdan çiqariladi.
    True  → muvaffaqiyatli yeçildi va unblock böldi.
    False → balans yetarli emas (kamida 1 kunga yetmaydi), foydalanuvçi hali ham blokli.
    Şartnoma rejimida bölsa — tölov olinmaydi, töğridan töğri unblock.
    """
    from django.utils import timezone
    from .models import StudentBalance, PaymentStatus, PaymentRecord, calculate_extension_from_amount

    try:
        balance = StudentBalance.objects.select_for_update().get(user=user)
        ps = PaymentStatus.objects.get(user=user)
        today = timezone.now().date()

        # Şartnoma rejimi — tölov olinmaydi, şunçaki unblock
        if balance.is_contract:
            user.is_blocked = False
            user.save(update_fields=['is_blocked'])
            ps.is_paid = True
            ps.last_payment_date = today
            ps.auto_blocked = False
            # next_payment_date belgilanmaydi (şartnoma abonent)
            balance.next_payment_date = None
            balance.save(update_fields=['next_payment_date', 'updated_at'])
            ps.save()
            return True

        monthly_fee = ps.monthly_fee or 200000
        if balance.amount <= 0:
            return False

        # Boşlanğiç sana: agar mavjud tölov muddati bugundan keyin bölsa, öşandan davom ettiramiz
        base_date = balance.next_payment_date if (balance.next_payment_date and balance.next_payment_date > today) else today

        next_date, added_days, used_amount, desc = calculate_extension_from_amount(
            balance.amount, monthly_fee, base_date=base_date
        )

        if not next_date or added_days < 1:
            return False

        # Balansdan yeçiş
        balance.amount -= used_amount
        balance.next_payment_date = next_date
        balance.save(update_fields=['amount', 'next_payment_date', 'updated_at'])

        # Unblock
        user.is_blocked = False
        user.save(update_fields=['is_blocked'])

        # PaymentStatus yangilaş
        ps.is_paid = True
        ps.last_payment_date = today
        ps.auto_blocked = False
        ps.paid_until = next_date
        ps.save()

        # PaymentRecord — yozuv
        PaymentRecord.objects.update_or_create(
            user=user, month=today.month, year=today.year,
            defaults={
                'is_paid': True,
                'paid_date': today,
                'paid_amount': used_amount,
                'amount': used_amount,
                'notes': f"Balansdan tölandi ({desc}). Keyingi tölov: {next_date.strftime('%d.%m.%Y')}",
            }
        )
        return True
    except (StudentBalance.DoesNotExist, PaymentStatus.DoesNotExist):
        return False
    except Exception:
        return False


def login_view(request):
    from django.conf import settings as django_settings

    if request.user.is_authenticated:
        return redirect('home')

    # Login urinişlari IP böyiça hisoblanadi. `REMOTE_ADDR` proxy ortida
    # hamma foydalanuvçi uçun bir xil bölib qolardi — bitta hujumçi
    # butun saytni qulflab qöyişi mumkin edi.
    ip_address = get_client_ip(request) or ''
    user_agent = request.META.get('HTTP_USER_AGENT', '')
    max_attempts = getattr(django_settings, 'LOGIN_MAX_ATTEMPTS', 5)
    lockout_minutes = getattr(django_settings, 'LOGIN_LOCKOUT_DURATION', 15)

    login_mode = request.POST.get('login_mode', 'username') if request.method == 'POST' else 'username'

    # Login token — sessiyadan oliş yoki yangi yaratiş
    login_token = request.session.get('login_token', '')
    if not login_token:
        login_token = LoginAttempt.generate_token()
        request.session['login_token'] = login_token

    if request.method == 'POST':
        form = LoginForm(request.POST if login_mode == 'username' else None)
        # login_token ATAYLAB POST dan olinmaydi. Ilgari
        #     login_token = request.POST.get('login_token', login_token)
        # deb yozilgan edi — hidden field mijoz nazoratida bölgani uçun
        # hujumçi har urinişda yangi tasodifiy token yuborib, pastdagi
        # `is_token_blocked` bloklaşini butunlay çetlab ötardi. Token faqat
        # serverdagi sessiyadan olinadi; hidden field template'da qolsa ham
        # işonilmaydi. Sessiyasiz brute-force uçun IP asosidagi
        # check_rate_limit işlaydi (uni cookie taşlab yuborib bölmaydi).
        request.session['login_token'] = login_token

        # 🛡️ IP-based rate limiting
        if login_mode == 'email':
            login_identifier = 'email:' + request.session.get('email_login_token', login_token)[:64]
        elif login_mode == 'telegram':
            # Telegram kodi — akkaunt identifikatori EMAS, balki taxmin
            # qilinayotgan sirning özi. Uni identifier qilib berilganda har
            # taxmin yangi kalitga tuşib, id_limit heç qaçon işlamaydi.
            # Sessiyaga boğlab qöyamiz — şunda 5 ta notöğri kodda töxtaydi.
            login_identifier = f'tgcode:{login_token[:32]}'
        else:
            login_identifier = (
                request.POST.get('username', '') or
                request.POST.get('phone', '') or
                ''
            ).strip().lower()
        blocked, block_msg = check_rate_limit(
            request, 'login', identifier=login_identifier,
            ip_limit=15, id_limit=5, window=900
        )
        if blocked:
            messages.error(request, block_msg)
            return _login_render(request, form, login_mode)

        # 🤖 Honeypot — botlar uçun tuzoq
        honeypot = request.POST.get('website_url', '')
        if honeypot:
            _record_failed_login(ip_address, 'honeypot_bot', user_agent, login_token)
            time.sleep(1.5)  # Bot sekinlaştiriş
            messages.error(request, "Kiriş rad etildi! ❌")
            return _login_render(request, form, login_mode)

        # 🔒 Token bloklangan bölsa — kirişga ruxsat bermaslik
        if LoginAttempt.is_token_blocked(login_token, max_attempts, lockout_minutes):
            remaining = LoginAttempt.get_token_remaining_time(login_token, max_attempts, lockout_minutes)
            remaining_min = remaining // 60 + 1
            messages.error(request, f"Juda köp notöğri uriniş! {remaining_min} daqiqadan keyin qayta urinib köring. 🔒")
            # Admin'ga xabar beriş
            if not request.session.get('brute_notified'):
                try:
                    notify_admins(
                        f"🚨 <b>Brute-force hujumi aniqlandi!</b>\n\n"
                        f"🌐 IP: {ip_address}\n"
                        f"🔑 Token: {login_token[:16]}...\n"
                        f"🕐 {timezone.localtime().strftime('%d.%m.%Y %H:%M')}\n"
                        f"📱 UA: {user_agent[:100]}\n"
                        f"⚠️ {max_attempts} marta notöğri uriniş — token bloklandi ({lockout_minutes} daqiqa)"
                    )
                except Exception:
                    pass
                request.session['brute_notified'] = True
            return _login_render(request, form, login_mode)

        # Email codes use the same successful-login/session flow as other methods.
        if login_mode == 'email':
            from .email_login import request_code, verify_code
            if request.POST.get('email_action') == 'send':
                messages.info(request, request_code(request))
                return _login_render(request, form, 'email')
            user = verify_code(request)
            if user is None:
                _record_failed_login(ip_address, 'email_code:invalid', user_agent, login_token)
                messages.error(request, 'Kod notöğri, muddati tugagan yoki işlatilgan. Yangi kod sörang.')
                return _login_render(request, form, 'email')

        # Telegram kod orqali login
        elif login_mode == 'telegram':
            telegram_code = request.POST.get('telegram_code', '').strip()
            if not telegram_code:
                messages.error(request, "Iltimos, kodni kiriting! ⚠️")
                return _login_render(request, form, 'telegram')

            # Faqat 6 xonali raqam — bazaga tegmasdan şovqinni tösadi.
            if not (len(telegram_code) == 6 and telegram_code.isdigit()):
                _record_failed_login(ip_address, 'telegram_code:invalid', user_agent, login_token)
                messages.error(request, "Kod notöğri yoki allaqaçon işlatilgan! ❌")
                return _login_render(request, form, 'telegram')

            from telegram_bot.models import TelegramLoginCode

            # Kodni bir martalik qilib qulflab olamiz: bir vaqtda kelgan ikki
            # sörov bitta kodni ikki marta "işlatib" yubormasin.
            with transaction.atomic():
                login_code = (
                    TelegramLoginCode.objects
                    .select_for_update()
                    .filter(code=telegram_code, is_used=False)
                    .first()
                )
                if login_code is None:
                    _record_failed_login(
                        ip_address, 'telegram_code:unknown', user_agent, login_token
                    )
                    messages.error(request, "Kod notöğri yoki allaqaçon işlatilgan! ❌")
                    return _login_render(request, form, 'telegram')

                if login_code.is_attempt_exhausted:
                    login_code.is_used = True   # kodni butunlay ölik qilamiz
                    login_code.save(update_fields=['is_used'])
                    messages.error(request, "Bu kod bloklandi. Botdan yangi kod oling. 🔒")
                    return _login_render(request, form, 'telegram')

                if login_code.is_expired:
                    messages.error(request, "Kod muddati tugagan! Botdan yangi kod oling. ⏰")
                    return _login_render(request, form, 'telegram')

                user = CustomUser.objects.filter(telegram_id=login_code.telegram_id).first()
                if user is None:
                    login_code.attempts += 1
                    login_code.save(update_fields=['attempts'])
                    messages.error(request, "Kod notöğri yoki allaqaçon işlatilgan! ❌")
                    return _login_render(request, form, 'telegram')

                login_code.is_used = True
                login_code.save(update_fields=['is_used'])

        elif login_mode == 'phone':
            phone = request.POST.get('phone', '').strip()
            password = request.POST.get('password', '').strip()

            if not phone or not password:
                messages.error(request, "Iltimos, barça maydonlarni töldiring ⚠️")
                return _login_render(request, form, 'phone')

            try:
                phone_user = CustomUser.objects.get(phone=phone)
                user = authenticate(request, username=phone_user.username, password=password)
            except CustomUser.DoesNotExist:
                user = None

            if user is None:
                _record_failed_login(ip_address, f'phone:{phone[:20]}', user_agent, login_token)
                messages.error(request, "Telefon raqam yoki parol notöğri ❌")
                return _login_render(request, form, 'phone')

        elif form.is_valid():
            username = form.cleaned_data.get('username')
            password = form.cleaned_data.get('password')

            user = authenticate(request, username=username, password=password)

            if user is None:
                # Parol töğri, lekin akkaunt öchirilgan/bloklangan (is_active=False)
                # bölsa — authenticate None qaytaradi. Şuni aniqlab, aniq xabar beramiz.
                _u = CustomUser.objects.filter(username__iexact=(username or '').strip()).first()
                if _u and not _u.is_active and _u.check_password(password):
                    messages.error(
                        request,
                        "Akkauntingiz bloklangan. Iltimos, administrator bilan boğlaning. ⛔"
                    )
                    return _login_render(request, form, login_mode)
                _record_failed_login(ip_address, username, user_agent, login_token)
                messages.error(request, "Username yoki parol notöğri ❌")
                return _login_render(request, form, login_mode)
        else:
            messages.error(request, "Iltimos, barça maydonlarni töğri töldiring ⚠️")
            return _login_render(request, form, login_mode)

        return _complete_login(request, user, ip_address, user_agent, login_token)

    else:
        form = LoginForm()

    return _login_render(request, form, 'username')


def _complete_login(request, user, ip_address, user_agent, login_token):
    """Foydalanuvçi tasdiqlangandan keyingi umumiy login jarayoni.

    Parol, telefon, email kodi, Telegram kodi va Google orqali kiriş — barçasi
    şu yerdan ötadi: bloklash/tölov, qurilma va sessiya, bonuslar, xabarlar.
    """
    # ⛔ Bloklangan user — avval balansdan avtomatik tölov sinab köriladi
    if hasattr(user, "is_blocked") and user.is_blocked:
        if not _auto_deduct_and_unblock(user):
            # Balans yetarli emas → login qilinadi, tölov sahifasiga yönaltiriladi
            _record_successful_login(ip_address, user.username, user_agent, login_token)
            request.session.pop('login_token', None)
            request.session.pop('brute_notified', None)
            login(request, user, backend=getattr(user, 'backend', 'django.contrib.auth.backends.ModelBackend'))
            user.last_activity = timezone.now()
            user.save(update_fields=["last_activity"])
            # Sessiyani röyxatdan ötkazamiz — aks holda DeviceLimitMiddleware
            # faol UserSession topa olmay, öquvçini /tolov/ ga yetmasdan
            # çiqarib yuboradi (tölov sahifasi faqat bir marta körinardi).
            _register_device_and_session(request, user)
            _reason = getattr(user, 'blocked_reason', '') or ''
            messages.warning(
                request,
                "Akkauntingiz vaqtinça bloklangan"
                + (f": {_reason}" if _reason else "")
                + ". Tölovni amalga oşiring. 🔒"
            )
            return redirect('payment_wall')
        # Unblock böldi — oddiy login davom etadi

    # ✅ Login successful — urinişlarni tozalaş
    _record_successful_login(ip_address, user.username, user_agent, login_token)
    # Token va brute_notified ni tozalaş
    request.session.pop('login_token', None)
    request.session.pop('brute_notified', None)
    login(request, user, backend=getattr(user, 'backend', 'django.contrib.auth.backends.ModelBackend'))

    user.last_activity = timezone.now()
    user.save(update_fields=["last_activity"])

    # 🔥 Kunlik streak'ni yangilaş
    try:
        user.update_streak()
    except Exception:
        pass

    # 📱 Qurilma va sessiya röyxatdan ötkaziş
    _register_device_and_session(request, user)

    # 💎 Premium kunlik bonus: 300 coin
    from .coin_rules import award_premium_daily_bonus
    bonus = award_premium_daily_bonus(user)
    if bonus:
        messages.success(request, "💎 Premium bonus: +{0} coin qöşildi!".format(bonus))

    ip_address = get_client_ip(request) or ''
    ActivityLog.objects.create(
        user=user,
        action_type='login',
        description="Tizimga kirdi",
        ip_address=ip_address
    )

    if user.telegram_id:
        ua = request.META.get('HTTP_USER_AGENT', '')
        if 'iPhone' in ua or 'iPad' in ua:
            device = '📱 iPhone/iPad'
        elif 'Android' in ua:
            device = '📱 Android'
        elif 'Windows' in ua:
            device = '💻 Windows'
        elif 'Macintosh' in ua or 'Mac OS' in ua:
            device = '💻 MacOS'
        elif 'Linux' in ua:
            device = '💻 Linux'
        else:
            device = '🌐 Noma\'lum qurilma'

        if 'Chrome' in ua and 'Edg' not in ua:
            browser = 'Chrome'
        elif 'Firefox' in ua:
            browser = 'Firefox'
        elif 'Safari' in ua and 'Chrome' not in ua:
            browser = 'Safari'
        elif 'Edg' in ua:
            browser = 'Edge'
        elif 'Opera' in ua or 'OPR' in ua:
            browser = 'Opera'
        else:
            browser = ''

        notify_user(
            user.telegram_id,
            f"🔐 <b>Hisobingizga kirildi!</b>\n\n"
            f"👤 {user.full_name}\n"
            f"🖥 Qurilma: {device}\n"
            f"{'🌍 Brauzer: ' + browser + chr(10) if browser else ''}"
            f"🌐 IP: {ip_address}\n"
            f"🕐 {timezone.localtime().strftime('%d.%m.%Y %H:%M')}\n\n"
            f"⚠️ Agar bu siz bölmasangiz, darhol parolingizni özgartiring!"
        )

    messages.success(request, "Tizimga muvaffaqiyatli kirdingiz! 🎉")

    # Premium reklama (premium bölmagan studentlar uçun)
    if user.role == 'student' and not user.is_premium:
        request.session['show_premium_ad'] = True

    # AI yordamçi reklama (har safar login qilganda)
    request.session['show_ai_ad'] = True

    # Har login'da qayta söraladi — ötkazib yuboriş (skip) belgisini tozalaymiz
    request.session.pop('profile_verify_skipped', None)

    # Geolokatsiya muvaffaqiyatli login'dan keyin, alohida rozilik bilan olinadi.
    # Brauzerda saqlangan qurilma roziligi 30 kun amal qiladi.
    # Boşqa zarur login oqimlari (tölov, profil, sörovnoma) ustuvor qoladi.
    request.session['location_capture_pending'] = True

    # Profil töliq bölmasa complete_profile sahifasiga yönaltiriş
    # (hujjat tasdiği — pasport yoki metrika — şu formaning içida)
    if user.role == 'student' and not user.is_profile_completed:
        request.session.pop('location_capture_pending', None)
        return redirect('complete_profile')

    # Sörovnoma tekşiriş (login qilganda)
    pending_survey = SurveyForm.objects.filter(
        is_active=True,
        show_on_login=True,
    ).exclude(
        responses__user=user
    ).filter(
        models.Q(target_role='all') | models.Q(target_role=user.role)
    ).first()
    if pending_survey:
        request.session.pop('location_capture_pending', None)
        return redirect('fill_survey', pk=pending_survey.pk)

    return redirect('login_location_consent')


@login_required
def login_location_consent(request):
    """Login'dan keyingi rozilik oynasi; mavjud redirect manzili saqlanadi."""
    if not request.session.get('location_capture_pending', False):
        return redirect('home')
    try:
        consent_expires_at = int(request.COOKIES.get('location_device_consent_until', '0'))
    except (TypeError, ValueError):
        consent_expires_at = 0
    if consent_expires_at > int(time.time()):
        request.session.pop('location_capture_pending', None)
        return redirect(post_login_target(request))
    return render(request, 'accounts/login_location_consent.html', {
        # Masalan, Windows ilovasiga kirişni tasdiqlaş sahifasiga qaytiş
        'post_login_url': peek_post_login_target(request, reverse('home')),
    })


@login_required
def login_location_skip(request):
    """Joylaşuvni ulaşmasdan login jarayonini davom ettiradi."""
    request.session.pop('location_capture_pending', None)
    return redirect(post_login_target(request))


@login_required
@require_POST
def login_location_record(request):
    """Brauzer ruxsati bilan olingan koordinatani joriy foydalanuvçiga boğlaydi."""
    if not request.session.get('location_capture_pending', False):
        return JsonResponse({'ok': False, 'error': 'Joylaşuv yozuvi uçun login tasdiği topilmadi.'}, status=403)
    if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
        return JsonResponse({'ok': False, 'error': 'Notöğri sörov.'}, status=400)
    try:
        latitude = float(request.POST.get('latitude', ''))
        longitude = float(request.POST.get('longitude', ''))
    except (TypeError, ValueError):
        return JsonResponse({'ok': False, 'error': 'Koordinatalar notöğri.'}, status=400)
    try:
        accuracy = float(request.POST.get('accuracy', ''))
        if not math.isfinite(accuracy) or accuracy < 0:
            accuracy = None
    except (TypeError, ValueError):
        accuracy = None
    if not (math.isfinite(latitude) and math.isfinite(longitude) and -90 <= latitude <= 90 and -180 <= longitude <= 180):
        return JsonResponse({'ok': False, 'error': 'Koordinatalar diapazondan taşqarida.'}, status=400)
    from decimal import Decimal
    lat = Decimal(str(round(latitude, 6)))
    lon = Decimal(str(round(longitude, 6)))
    map_url = f'https://www.google.com/maps?q={lat},{lon}'
    location = LoginLocation.objects.create(
        user=request.user,
        latitude=lat,
        longitude=lon,
        map_url=map_url,
        # Tarmoq orqali aniqlangan joylaşuv ba'zan juda katta radius qaytaradi.
        # Bu joylaşuv yozuvini rad etmasin; bazada yuqori çegaraga qisqartiramiz.
        accuracy_meters=min(int(round(accuracy)), 100000) if accuracy is not None else None,
    )
    request.session.pop('location_capture_pending', None)
    # Qaytiş manzili sahifaga allaqaçon yozilgan (post_login_url) — sessiyada qolib ketmasin
    request.session.pop(POST_LOGIN_SESSION_KEY, None)
    address = ''
    try:
        import requests
        response = requests.get(
            'https://nominatim.openstreetmap.org/reverse',
            params={'format': 'jsonv2', 'lat': str(lat), 'lon': str(lon), 'zoom': 16, 'addressdetails': 1},
            headers={'User-Agent': 'ITCreativeLMS/1.0 (login location lookup)'},
            timeout=2.5,
        )
        if response.ok:
            payload = response.json()
            parts = payload.get('address') or {}
            locality = (
                parts.get('city') or parts.get('town') or parts.get('village')
                or parts.get('municipality') or parts.get('county') or ''
            )
            district = parts.get('county') or parts.get('district') or ''
            region = parts.get('state') or parts.get('region') or ''
            address = ', '.join(dict.fromkeys(part for part in (locality, district, region, parts.get('country')) if part))
            if not address:
                address = (payload.get('display_name') or '')[:255]
    except Exception:
        # Reverse geocoding taşqi xizmatga boğliq; koordinata va map link saqlanib qoladi.
        pass
    if address:
        location.address = address[:255]
        location.save(update_fields=['address'])
    response = JsonResponse({'ok': True, 'address': address})
    consent_max_age = 30 * 24 * 60 * 60
    response.set_cookie(
        'location_device_consent_until', str(int(time.time()) + consent_max_age), max_age=consent_max_age,
        httponly=True, secure=request.is_secure(), samesite='Lax', path='/',
    )
    return response


def logout_view(request):
    if request.user.is_authenticated:
        # Sessiyani deaktivatsiya qiliş
        current_session_key = request.session.session_key
        if current_session_key:
            UserSession.objects.filter(
                user=request.user, session_key=current_session_key
            ).update(is_active=False)

        ActivityLog.objects.create(
            user=request.user,
            action_type='logout',
            description="Tizimdan çiqdi",
            ip_address=get_client_ip(request)
        )

    logout(request)
    messages.success(request, "Tizimdan muvaffaqiyatli çiqdingiz 👋")
    return redirect('login')


# ============================================================
# ROLGA XOS BOŞ SAHIFALAR (home_view içidan çaqiriladi)
# ============================================================

def _safe_photo_url(obj):
    """Rasm URL'ini xavfsiz oliş (fayl öçirilgan bölsa ham 500 bermaydi)."""
    try:
        return obj.photo.url if obj.photo else ''
    except Exception:
        return ''


def _build_quick_actions(items):
    """(label, url_name, icon) röyxatidan xavfsiz quick-action röyxati."""
    out = []
    for label, url_name, icon in items:
        try:
            out.append({'label': label, 'url': reverse(url_name), 'icon': icon})
        except Exception:
            continue
    return out


_WEEKDAY_UZ = ['Du', 'Se', 'Ç', 'Pa', 'Ju', 'Ş', 'Ya']


def _week_bars(date_counts, today):
    """Oxirgi 7 kun uçun mini bar-chart ma'lumoti: {label, count, pct}."""
    bars = []
    try:
        max_c = max(date_counts.values(), default=0) or 1
        for i in range(6, -1, -1):
            d = today - timedelta(days=i)
            c = date_counts.get(d, 0)
            bars.append({
                'label': _WEEKDAY_UZ[d.weekday()],
                'count': c,
                'pct': max(4, round(c / max_c * 100)) if c else 4,
            })
    except Exception:
        bars = []
    return bars


def _trend(current, previous):
    """Oldingi davr bilan soliştiriş: {dir, pct}. previous=0 bölsa yönaliş beriladi."""
    try:
        cur, prev = int(current or 0), int(previous or 0)
    except (TypeError, ValueError):
        return None
    if not prev:
        return {'dir': 'up', 'pct': 100} if cur else None
    pct = round((cur - prev) / prev * 100)
    if pct == 0:
        return {'dir': 'flat', 'pct': 0}
    return {'dir': 'up' if pct > 0 else 'down', 'pct': abs(pct)}


def _online_now(minutes=5):
    """Oxirgi `minutes` daqiqada faol bölgan noyob foydalanuvçilar soni."""
    try:
        return UserSession.objects.filter(
            is_active=True,
            last_activity__gte=timezone.now() - timedelta(minutes=minutes),
        ).values('user').distinct().count()
    except Exception:
        return 0


def _home_teacher(request):
    """Öqituvçi uçun boş sahifa: kurslari, tekşirilmagan vazifalar, faollik."""
    user = request.user
    professions = user.teaching_professions.annotate(
        lesson_total=Count('lessons', distinct=True),
        student_total=Count(
            'enrollments',
            filter=Q(enrollments__user__role='student'),
            distinct=True,
        ),
    )
    my_courses = []
    for p in professions[:8]:
        my_courses.append({
            'id': p.pk,
            'name': p.name,
            'photo': _safe_photo_url(p),
            'lesson_total': p.lesson_total,
            'student_total': p.student_total,
        })

    total_students = CourseEnrollment.objects.filter(
        profession__teachers=user, user__role='student'
    ).values('user').distinct().count()

    pending_homeworks = HomeworkSubmission.objects.filter(
        homework__lesson__profession__teachers=user, status='pending'
    ).count()

    pending_speaking = 0
    try:
        pending_speaking = SpeakingSubmission.objects.filter(
            speaking_task__lesson__profession__teachers=user, status='pending'
        ).count()
    except Exception:
        pending_speaking = 0

    today = timezone.localdate()
    tests_today = TestResult.objects.filter(
        test__lesson__profession__teachers=user, completed_at__date=today
    ).count()

    recent_submissions = list(
        HomeworkSubmission.objects.filter(
            homework__lesson__profession__teachers=user
        ).select_related(
            'student', 'homework', 'homework__lesson'
        ).order_by('-submitted_at')[:6]
    )

    # Sönggi test natijalari
    recent_tests = list(
        TestResult.objects.filter(
            test__lesson__profession__teachers=user
        ).select_related('student', 'test', 'test__lesson').order_by('-completed_at')[:6]
    )

    # Oxirgi 7 kunda topşirilgan vazifalar (mini-chart)
    try:
        _counts = dict(
            HomeworkSubmission.objects.filter(
                homework__lesson__profession__teachers=user,
                submitted_at__date__gte=today - timedelta(days=6),
            ).values_list('submitted_at__date').annotate(c=Count('id')).values_list('submitted_at__date', 'c')
        )
    except Exception:
        _counts = {}
    week_bars = _week_bars(_counts, today)
    week_total = sum(_counts.values())

    # Eng faol öquvçilar (coin böyiça, faqat öz kurslaridan)
    top_students = list(
        CustomUser.objects.filter(
            role='student', enrollments__profession__teachers=user
        ).distinct().order_by('-coins')[:5]
    )

    # ── Bugungi körsatkiçlar ──
    subs_today = HomeworkSubmission.objects.filter(
        homework__lesson__profession__teachers=user, submitted_at__date=today
    ).count()
    try:
        students_active_today = ActivityLog.objects.filter(
            user__enrollments__profession__teachers=user, created_at__date=today
        ).values('user').distinct().count()
    except Exception:
        students_active_today = 0

    today_stats = [
        {'label': "Bugun topşirilgan vazifa", 'value': subs_today, 'icon': 'journal-arrow-up'},
        {'label': "Bugungi test natijasi", 'value': tests_today, 'icon': 'patch-check'},
        {'label': "Bugun faol öquvçilar", 'value': students_active_today, 'icon': 'person-check'},
    ]

    # ── Sifat körsatkiçlari (oxirgi 30 kun) ──
    since_30 = timezone.now() - timedelta(days=30)
    score_agg = TestResult.objects.filter(
        test__lesson__profession__teachers=user, completed_at__gte=since_30
    ).aggregate(
        avg=Avg('score'), total=Count('id'), ok=Count('id', filter=Q(passed=True))
    )
    avg_score = round(score_agg['avg'] or 0)
    tests_month = score_agg['total'] or 0
    pass_rate = round((score_agg['ok'] or 0) / tests_month * 100) if tests_month else 0

    hw_total = HomeworkSubmission.objects.filter(
        homework__lesson__profession__teachers=user
    ).count()
    graded_pct = round((hw_total - pending_homeworks) / hw_total * 100) if hw_total else 100

    # ── Oldingi hafta bilan soliştiriş ──
    try:
        prev_week_subs = HomeworkSubmission.objects.filter(
            homework__lesson__profession__teachers=user,
            submitted_at__date__gte=today - timedelta(days=13),
            submitted_at__date__lt=today - timedelta(days=6),
        ).count()
    except Exception:
        prev_week_subs = 0
    week_trend = _trend(week_total, prev_week_subs)

    # ── E'tibor talab qiladigan öquvçilar (7+ kun faolsiz) ──
    risk_cutoff = timezone.now() - timedelta(days=7)
    risk_students = []
    try:
        for s in CustomUser.objects.filter(
            role='student', enrollments__profession__teachers=user
        ).exclude(last_login__gte=risk_cutoff).distinct().order_by('last_login')[:5]:
            risk_students.append({
                'name': s.get_full_name() or s.username,
                'days': (timezone.now() - s.last_login).days if s.last_login else None,
            })
    except Exception:
        risk_students = []

    quick_actions = _build_quick_actions([
        ("Kurslarim", 'teacher:courses', 'collection-play'),
        ("Öquvçilarim", 'teacher:my_students', 'people'),
        ("Vazifalar", 'teacher:homeworks', 'journal-check'),
        ("Test natijalari", 'teacher:test_results', 'patch-check'),
        ("Speaking", 'teacher:speaking_tasks', 'mic'),
        ("Guruh çatlari", 'teacher:group_chats', 'chat-dots'),
        ("Daromadlarim", 'teacher:earnings', 'wallet2'),
        ("Sertifikatlar", 'teacher:certificates', 'award'),
        ("Statistika", 'teacher:statistics', 'graph-up'),
        ("AI tekşiruvçi", 'teacher:ai_checker', 'robot'),
    ])

    stat_cards = [
        {'label': "Kurslarim", 'value': len(my_courses), 'icon': 'collection-play', 'color': 'accent'},
        {'label': "Öquvçilarim", 'value': total_students, 'icon': 'people', 'color': 'accent2'},
        {'label': "Tekşirilmagan vazifa", 'value': pending_homeworks, 'icon': 'journal-x', 'color': 'warning'},
        {'label': "Bugungi test natijalari", 'value': tests_today, 'icon': 'patch-check', 'color': 'success'},
    ]

    return render(request, 'accounts/home_teacher.html', {
        'my_courses': my_courses,
        'stat_cards': stat_cards,
        'pending_homeworks': pending_homeworks,
        'pending_speaking': pending_speaking,
        'recent_submissions': recent_submissions,
        'recent_tests': recent_tests,
        'week_bars': week_bars,
        'week_total': week_total,
        'week_trend': week_trend,
        'top_students': top_students,
        'quick_actions': quick_actions,
        'today_stats': today_stats,
        'avg_score': avg_score,
        'pass_rate': pass_rate,
        'tests_month': tests_month,
        'graded_pct': graded_pct,
        'risk_students': risk_students,
        'online_now': _online_now(),
    })


def _home_admin(request):
    """Admin uçun boş sahifa: umumiy statistika, kutilayotgan işlar, faollik."""
    stats = get_performance_cache().get('home_admin_stats')
    if stats is None:
        stats = {
            'students': CustomUser.objects.filter(role='student').count(),
            'teachers': CustomUser.objects.filter(role='teacher').count(),
            'courses': Profession.objects.count(),
            'lessons': Lesson.objects.count(),
            'blocked': CustomUser.objects.filter(is_blocked=True).count(),
            'enrollments': CourseEnrollment.objects.count(),
        }
        get_performance_cache().set('home_admin_stats', stats, 120)

    week_ago = timezone.now() - timedelta(days=7)
    new_users_week = CustomUser.objects.filter(date_joined__gte=week_ago).count()

    pending_items = [
        {'label': "Tekşirilmagan uy vazifalar", 'count': HomeworkSubmission.objects.filter(status='pending').count(),
         'url_name': 'admin_dashboard', 'icon': 'journal-x'},
        {'label': "Yordam sörovlari", 'count': HelpRequest.objects.filter(status='pending').count(),
         'url_name': 'admin_help_requests', 'icon': 'life-preserver'},
        {'label': "Yangi arizalar", 'count': CourseApplication.objects.filter(status='new').count(),
         'url_name': 'admin_applications', 'icon': 'inbox'},
        {'label': "Kursga yoziliş sörovlari", 'count': CourseEnrollmentRequest.objects.filter(status='pending').count(),
         'url_name': 'admin_enrollment_requests', 'icon': 'person-plus'},
        {'label': "Premium sörovlari", 'count': PremiumRequest.objects.filter(status='pending').count(),
         'url_name': 'admin_premium_management', 'icon': 'gem'},
    ]
    pending = []
    for item in pending_items:
        try:
            item['url'] = reverse(item['url_name'])
        except Exception:
            item['url'] = '#'
        pending.append(item)
    pending_total = sum(i['count'] for i in pending)

    recent_users = list(
        CustomUser.objects.order_by('-date_joined')[:6]
    )
    recent_activity = list(
        ActivityLog.objects.select_related('user').order_by('-created_at')[:8]
    )

    today = timezone.localdate()
    logins_today = ActivityLog.objects.filter(
        action_type='login', created_at__date=today
    ).count()

    # Oxirgi 7 kunda röyxatdan ötişlar (mini-chart)
    try:
        _counts = dict(
            CustomUser.objects.filter(
                date_joined__date__gte=today - timedelta(days=6)
            ).values_list('date_joined__date').annotate(c=Count('id')).values_list('date_joined__date', 'c')
        )
    except Exception:
        _counts = {}
    week_bars = _week_bars(_counts, today)

    # Eng ommabop kurslar (öquvçilar soni böyiça)
    top_courses = list(
        Profession.objects.annotate(
            student_total=Count(
                'enrollments',
                filter=Q(enrollments__user__role='student'),
                distinct=True,
            )
        ).order_by('-student_total')[:5]
    )

    # Sönggi yangi arizalar
    recent_applications = list(
        CourseApplication.objects.filter(status='new').order_by('-created_at')[:5]
    )

    # ── Oldingi hafta bilan soliştiriş ──
    prev_week_users = CustomUser.objects.filter(
        date_joined__gte=timezone.now() - timedelta(days=14), date_joined__lt=week_ago
    ).count()
    users_trend = _trend(new_users_week, prev_week_users)

    # ── Bugungi harakat körsatkiçlari ──
    def _count_today(qs_filter):
        try:
            return qs_filter()
        except Exception:
            return 0

    coins_today = _count_today(lambda: CoinTransaction.objects.filter(
        created_at__date=today, action='add'
    ).aggregate(s=Sum('amount'))['s'] or 0)

    today_stats = [
        {'label': "Yangi röyxatlar", 'value': CustomUser.objects.filter(date_joined__date=today).count(),
         'icon': 'person-plus'},
        {'label': "Körilgan videolar", 'value': _count_today(
            lambda: VideoProgress.objects.filter(watched_at__date=today).count()), 'icon': 'play-btn'},
        {'label': "Topşirilgan vazifalar", 'value': HomeworkSubmission.objects.filter(
            submitted_at__date=today).count(), 'icon': 'journal-arrow-up'},
        {'label': "Test natijalari", 'value': TestResult.objects.filter(
            completed_at__date=today).count(), 'icon': 'patch-check'},
        {'label': "Berilgan coinlar", 'value': coins_today, 'icon': 'coin'},
    ]

    # ── Eng köp öquvçisi bor öqituvçilar ──
    top_teachers = []
    try:
        top_teachers = list(
            CustomUser.objects.filter(role='teacher').annotate(
                student_total=Count(
                    'teaching_professions__enrollments__user',
                    filter=Q(teaching_professions__enrollments__user__role='student'),
                    distinct=True,
                ),
                course_total=Count('teaching_professions', distinct=True),
            ).filter(course_total__gt=0).order_by('-student_total')[:5]
        )
    except Exception:
        top_teachers = []

    # ── Tizim salomatligi ──
    empty_courses = _count_today(
        lambda: Profession.objects.annotate(n=Count('lessons')).filter(n=0).count())
    failed_logins = _count_today(lambda: LoginAttempt.objects.filter(
        was_successful=False, attempted_at__date=today).count())
    active_sessions = _count_today(
        lambda: UserSession.objects.filter(is_active=True).count())

    health = [
        {'label': "Darssiz kurslar", 'value': empty_courses, 'icon': 'folder-x',
         'state': 'ok' if not empty_courses else 'warn'},
        {'label': "Bugungi muvaffaqiyatsiz kirişlar", 'value': failed_logins, 'icon': 'shield-exclamation',
         'state': 'ok' if failed_logins < 10 else 'warn'},
        {'label': "Bloklangan foydalanuvçilar", 'value': stats['blocked'], 'icon': 'slash-circle',
         'state': 'ok' if not stats['blocked'] else 'warn'},
        {'label': "Faol sessiyalar", 'value': active_sessions, 'icon': 'hdd-network', 'state': 'ok'},
    ]

    quick_actions = _build_quick_actions([
        ("Foydalanuvçilar", 'admin_users', 'people'),
        ("Öqituvçi qöşiş", 'admin_add_teacher', 'person-plus'),
        ("Kurs qöşiş", 'admin_profession_add', 'folder-plus'),
        ("Kurslar", 'admin_professions', 'collection-play'),
        ("Coin boşqaruvi", 'admin_manage_coins', 'coin'),
        ("Xabarlar", 'admin_messages', 'envelope'),
        ("Sörovnomalar", 'admin_surveys', 'clipboard-data'),
        ("Kutubxona", 'admin_books', 'book'),
        ("Xavfsizlik", 'admin_security_events', 'shield-lock'),
        ("Davomat", 'admin_teacher_attendance', 'calendar-check'),
    ])

    stat_cards = [
        {'label': "Öquvçilar", 'value': stats['students'], 'icon': 'mortarboard', 'color': 'accent'},
        {'label': "Öqituvçilar", 'value': stats['teachers'], 'icon': 'person-video3', 'color': 'accent2'},
        {'label': "Kurslar", 'value': stats['courses'], 'icon': 'collection-play', 'color': 'success'},
        {'label': "Darslar", 'value': stats['lessons'], 'icon': 'journal-bookmark', 'color': 'accent'},
        {'label': "Yozilişlar", 'value': stats['enrollments'], 'icon': 'person-check', 'color': 'accent2'},
        {'label': "Bloklanganlar", 'value': stats['blocked'], 'icon': 'slash-circle', 'color': 'danger'},
    ]

    return render(request, 'accounts/home_admin.html', {
        'stat_cards': stat_cards,
        'pending': pending,
        'pending_total': pending_total,
        'new_users_week': new_users_week,
        'users_trend': users_trend,
        'recent_users': recent_users,
        'recent_activity': recent_activity,
        'logins_today': logins_today,
        'week_bars': week_bars,
        'top_courses': top_courses,
        'top_teachers': top_teachers,
        'recent_applications': recent_applications,
        'quick_actions': quick_actions,
        'today_stats': today_stats,
        'health': health,
        'online_now': _online_now(),
    })


def _home_assistant(request):
    """Yordamçi admin uçun boş sahifa: kutilayotgan işlar va tezkor havolalar."""
    pending_items = [
        {'label': "Uy vazifalar", 'count': HomeworkSubmission.objects.filter(status='pending').count(),
         'url_name': 'yordamchi_homework', 'icon': 'journal-x'},
        {'label': "Yordam sörovlari", 'count': HelpRequest.objects.filter(status='pending').count(),
         'url_name': 'yordamchi_help', 'icon': 'life-preserver'},
        {'label': "Yangi arizalar", 'count': CourseApplication.objects.filter(status='new').count(),
         'url_name': 'yordamchi_applications', 'icon': 'inbox'},
        {'label': "Boşqa sörovlar", 'count': (
            PasswordResetRequest.objects.filter(status='pending').count()
            + CourseEnrollmentRequest.objects.filter(status='pending').count()
            + CourseUnenrollRequest.objects.filter(status='pending').count()
            + PremiumRequest.objects.filter(status='pending').count()
        ), 'url_name': 'yordamchi_requests', 'icon': 'ui-checks'},
    ]
    pending = []
    for item in pending_items:
        try:
            item['url'] = reverse(item['url_name'])
        except Exception:
            item['url'] = '#'
        pending.append(item)
    pending_total = sum(i['count'] for i in pending)

    stats = get_performance_cache().get('home_assistant_stats')
    if stats is None:
        stats = {
            'students': CustomUser.objects.filter(role='student').count(),
            'teachers': CustomUser.objects.filter(role='teacher').count(),
            'parents': Parent.objects.count(),
            'blocked': CustomUser.objects.filter(is_blocked=True).count(),
        }
        get_performance_cache().set('home_assistant_stats', stats, 120)

    stat_cards = [
        {'label': "Öquvçilar", 'value': stats['students'], 'icon': 'mortarboard', 'color': 'accent'},
        {'label': "Öqituvçilar", 'value': stats['teachers'], 'icon': 'person-video3', 'color': 'accent2'},
        {'label': "Ota-onalar", 'value': stats['parents'], 'icon': 'people', 'color': 'success'},
        {'label': "Bloklanganlar", 'value': stats['blocked'], 'icon': 'slash-circle', 'color': 'danger'},
    ]

    today = timezone.localdate()

    # Bugungi körsatkiçlar
    today_stats = [
        {'label': "Bugungi arizalar", 'value': CourseApplication.objects.filter(created_at__date=today).count(),
         'icon': 'inbox'},
        {'label': "Bugun topşirilgan vazifalar", 'value': HomeworkSubmission.objects.filter(submitted_at__date=today).count(),
         'icon': 'journal-arrow-up'},
        {'label': "Bugungi yordam sörovlari", 'value': HelpRequest.objects.filter(created_at__date=today).count(),
         'icon': 'life-preserver'},
    ]

    # Keçikkan kitob ijaralari
    overdue_rentals = 0
    library = {'total': 0, 'rented': 0, 'available': 0}
    try:
        from .models import Book, BookRental
        overdue_rentals = BookRental.objects.exclude(status='returned').filter(
            return_date__lt=today
        ).count()
        library = {
            'total': Book.objects.count(),
            'rented': BookRental.objects.filter(status='active').count(),
            'available': Book.objects.aggregate(s=Sum('available_copies'))['s'] or 0,
        }
    except Exception:
        overdue_rentals = 0

    # Sönggi yangi arizalar
    recent_applications = list(
        CourseApplication.objects.filter(status='new').order_by('-created_at')[:5]
    )

    # Sönggi yordam sörovlari
    recent_help = list(
        HelpRequest.objects.select_related('user').filter(
            status='pending'
        ).order_by('-created_at')[:5]
    )

    # Oxirgi 7 kunlik arizalar grafigi
    try:
        _counts = dict(
            CourseApplication.objects.filter(
                created_at__date__gte=today - timedelta(days=6)
            ).values_list('created_at__date').annotate(c=Count('id')).values_list('created_at__date', 'c')
        )
    except Exception:
        _counts = {}
    week_bars = _week_bars(_counts, today)
    week_total = sum(_counts.values())

    # Mening sönggi harakatlarim (audit)
    my_actions = []
    my_week_total = 0
    try:
        from .models import AssistantActionLog
        my_actions = list(
            AssistantActionLog.objects.filter(user=request.user).order_by('-created_at')[:6]
        )
        my_week_total = AssistantActionLog.objects.filter(
            user=request.user, created_at__gte=timezone.now() - timedelta(days=7)
        ).count()
    except Exception:
        my_actions = []

    quick_actions = _build_quick_actions([
        ("Öquvçilar", 'yordamchi_students', 'mortarboard'),
        ("Öqituvçilar", 'yordamchi_teachers', 'person-video3'),
        ("Ota-onalar", 'yordamchi_parents', 'people'),
        ("Uy vazifalar", 'yordamchi_homework', 'journal-check'),
        ("Testlar", 'yordamchi_tests', 'patch-question'),
        ("Sertifikatlar", 'yordamchi_certificates', 'patch-check'),
        ("Kutubxona", 'yordamchi_books', 'book'),
        ("Ijaralar", 'yordamchi_rentals', 'arrow-left-right'),
        ("Keçikkan ijaralar", 'yordamchi_rentals_overdue', 'exclamation-triangle'),
        ("Kitob arizalari", 'yordamchi_book_requests', 'journal-plus'),
        ("Yordam sörovlari", 'yordamchi_help', 'life-preserver'),
        ("Harakatlar tarixi", 'yordamchi_audit_log', 'clock-history'),
    ])

    return render(request, 'accounts/home_assistant.html', {
        'stat_cards': stat_cards,
        'pending': pending,
        'pending_total': pending_total,
        'today_stats': today_stats,
        'overdue_rentals': overdue_rentals,
        'recent_applications': recent_applications,
        'recent_help': recent_help,
        'my_actions': my_actions,
        'my_week_total': my_week_total,
        'quick_actions': quick_actions,
        'week_bars': week_bars,
        'week_total': week_total,
        'library': library,
        'online_now': _online_now(),
    })


DAILY_QUESTION_REWARD = 3
_DQ_MARKER = 'Kun savoli'
_DQ_OK = '[correct]'
_DQ_FAIL = '[wrong]'

_DAILY_QUOTES = (
    ("Bugun qilgan kiçik qadamingiz — ertangi katta natijangiz.", "IT Creative"),
    ("Kod yozişni örganiş — fikrlaşni örganişdir.", "Steve Jobs"),
    ("Mukammallikni kutib ötirmang, boşlang va yölda örganing.", "Kent Beck"),
    ("Har kuni 1% yaxşilaniş — bir yilda 37 barobar ösiş.", "James Clear"),
    ("Eng yaxşi dasturçi — eng köp öqigan dasturçi.", "Robert C. Martin"),
    ("Xatolar — muvaffaqiyatga olib boruvçi eng qisqa yöl.", "Thomas Edison"),
    ("Bilim — yagona boylik, uni sarflaganingiz sari köpayadi.", "Xalq maqoli"),
    ("Murakkab masalani kiçik bölaklarga böl — u oddiy bölib qoladi.", "Rene Descartes"),
    ("Bugun örganmagan narsangizni ertaga kimdir sizdan yaxşi biladi.", "IT Creative"),
    ("Izçillik iste'doddan kuçli.", "Xalq maqoli"),
    ("Yaxşi kod — izohga muhtoj bölmagan kod.", "Martin Fowler"),
    ("Örganiş töxtaganda ösiş ham töxtaydi.", "Benjamin Franklin"),
    ("Sabr — bu kutiş emas, kutayotganda işlaşdir.", "Xalq maqoli"),
    ("Kiçik loyihalar katta dasturçilarni tarbiyalaydi.", "Linus Torvalds"),
    ("Bugungi maşqingiz — ertangi işonçingiz.", "IT Creative"),
)


def _daily_quote(today):
    text, author = _DAILY_QUOTES[today.toordinal() % len(_DAILY_QUOTES)]
    return {'text': text, 'author': author}


def _next_study_lesson(user):
    """Öquvçining keyingi jadval darsi — countdown uçun aniq vaqt bilan."""
    try:
        groups = list(StudyGroup.objects.running().filter(
            students=user
        ).select_related('profession', 'teacher'))
    except Exception:
        return None
    if not groups:
        return None

    now = timezone.localtime()
    tz = timezone.get_current_timezone()
    best = None
    for g in groups:
        for offset in range(0, 8):
            day = now.date() + timedelta(days=offset)
            if not g.has_day(day.isoweekday()):
                continue
            start_dt = timezone.make_aware(datetime.combine(day, g.start_time), tz)
            if start_dt <= now:
                continue
            if best is None or start_dt < best[0]:
                best = (start_dt, g)
            break

    if best is None:
        return None

    start_dt, group = best
    delta_days = (start_dt.date() - now.date()).days
    if delta_days == 0:
        day_label = 'Bugun'
    elif delta_days == 1:
        day_label = 'Ertaga'
    else:
        day_label = StudyGroup.DAYS_OF_WEEK[start_dt.isoweekday() - 1][1]
    try:
        teacher_name = group.teacher.get_full_name() if group.teacher else ''
    except Exception:
        teacher_name = ''
    return {
        'group_name': group.name,
        'course': group.profession.name,
        'teacher': teacher_name,
        'room': group.room,
        'color': group.color or '#6366f1',
        'start_iso': start_dt.isoformat(),
        'time_label': '%s–%s' % (group.start_time.strftime('%H:%M'), group.end_time.strftime('%H:%M')),
        'day_label': day_label,
    }


def _daily_question_for(user, today):
    """Kunning savoli — mavjud test savollaridan, foydalanuvçi+kun böyiça barqaror tanlov."""
    seed = (user.pk or 1) * 7919 + today.toordinal()
    try:
        prof_ids = list(user.enrollments.values_list('profession_id', flat=True))
    except Exception:
        prof_ids = []

    base = TestQuestion.objects.filter(answers__is_correct=True).distinct()
    pools = []
    if prof_ids:
        pools.append(base.filter(test__lesson__profession_id__in=prof_ids))
    pools.append(base)

    for pool in pools:
        pool = pool.order_by('id')
        try:
            total = pool.count()
        except Exception:
            total = 0
        if not total:
            continue
        idx = seed % total
        for step in range(min(5, total)):
            question = pool[(idx + step) % total:(idx + step) % total + 1].first()
            if not question:
                continue
            answers = list(question.answers.all())
            correct = next((a for a in answers if a.is_correct), None)
            if len(answers) < 2 or correct is None:
                continue
            options = [{'id': a.id, 'text': a.answer_text} for a in answers]
            random.Random(seed).shuffle(options)
            try:
                course_name = question.test.lesson.profession.name
            except Exception:
                course_name = ''
            try:
                image = question.question_image.url if question.question_image else ''
            except Exception:
                image = ''
            return {
                'id': question.id,
                'text': question.question_text,
                'image': image,
                'explanation': question.explanation or '',
                'course': course_name,
                'options': options,
                'correct_id': correct.id,
                'reward': DAILY_QUESTION_REWARD,
            }
    return None


def _daily_question_log(user, today):
    """Bugun kun savoliga javob berilganini bildiruvçi yozuv (yoki None)."""
    try:
        return ActivityLog.objects.filter(
            user=user,
            action_type='submit_test',
            description__startswith=_DQ_MARKER,
            created_at__date=today,
        ).order_by('-created_at').first()
    except Exception:
        return None


@login_required
@require_POST
def onboarding_done(request):
    """Öquvçi qöllanmasi tugatildi/ötkazib yuborildi — qayta avtomatik çiqmaydi."""
    CustomUser.objects.filter(pk=request.user.pk).update(onboarding_done=True)
    return JsonResponse({'ok': True})


@login_required
def daily_question_answer(request):
    """Kun savoliga javobni qabul qiladi: kunda faqat bir marta, töğri javobga coin."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'method'}, status=405)

    user = request.user
    today = timezone.localdate()
    question = _daily_question_for(user, today)
    if not question:
        return JsonResponse({'ok': False, 'error': 'no_question'}, status=404)

    previous = _daily_question_log(user, today)
    if previous:
        return JsonResponse({
            'ok': False,
            'error': 'already',
            'correct_id': question['correct_id'],
            'was_correct': _DQ_OK in previous.description,
            'explanation': question['explanation'],
        }, status=409)

    answer_id = _to_int(request.POST.get('answer_id'), 0)
    if answer_id not in {o['id'] for o in question['options']}:
        return JsonResponse({'ok': False, 'error': 'bad_answer'}, status=400)

    is_correct = answer_id == question['correct_id']
    reward = DAILY_QUESTION_REWARD if is_correct else 0

    ActivityLog.objects.create(
        user=user,
        action_type='submit_test',
        description='%s %s — %s' % (
            _DQ_MARKER,
            _DQ_OK if is_correct else _DQ_FAIL,
            question['text'][:120],
        ),
        ip_address=get_client_ip(request),
    )
    if reward:
        user.add_coins(reward, 'Kun savoli')

    return JsonResponse({
        'ok': True,
        'correct': is_correct,
        'correct_id': question['correct_id'],
        'explanation': question['explanation'],
        'reward': reward,
        'coins': user.coins,
    })


def home_view(request):
    # Landing sahifa — login qilmagan foydalanuvçilar uçun
    if not request.user.is_authenticated:
        public_data = landing_data()
        from .course_pricing import attach_prices
        attach_prices(public_data['professions'] + public_data['paid_courses'])
        success = False

        if request.method == 'POST':
            first_name = request.POST.get('first_name', '').strip()
            last_name = request.POST.get('last_name', '').strip()
            raw_phone = request.POST.get('phone', '').strip()
            phone_digits = ''.join(c for c in raw_phone if c.isdigit())
            if phone_digits.startswith('998') and len(phone_digits) == 12:
                phone_digits = phone_digits[3:]
            phone = '+998' + phone_digits if phone_digits else raw_phone
            course_interest = request.POST.get('course_interest', '').strip()

            if first_name and last_name and phone:
                CourseApplication.objects.create(
                    first_name=first_name,
                    last_name=last_name,
                    phone=phone,
                    course_interest=course_interest
                )
                notify_application_recipients(
                    f"📋 <b>Yangi ariza keldi!</b>\n\n"
                    f"👤 {first_name} {last_name}\n"
                    f"📱 {phone}\n"
                    f"📚 Kurs: {course_interest or 'Belgilanmagan'}\n\n"
                    f"📞 Tez orada boğlaning!"
                )
                success = True

        from coin.topup_pricing import promos
        return render(request, 'landing.html', {
            **public_data,
            'coin_promos': promos(),
            'success': success,
        })

    # --- Rolga qarab boş sahifa: har bir rol öz home körinişini köradi ---
    #   admin           -> accounts/home_admin.html
    #   assistant_admin -> accounts/home_assistant.html
    #   teacher         -> accounts/home_teacher.html
    #   student         -> quyidagi premium home (accounts/home.html)
    if request.user.role == 'admin':
        return _home_admin(request)
    if request.user.role == 'assistant_admin':
        return _home_assistant(request)
    if request.user.role == 'teacher':
        return _home_teacher(request)

    # --- Asosiy ma'lumotlar (login qilgan öquvçilar uçun) ---
    professions = Profession.objects.prefetch_related('teachers').annotate(
        lesson_count=Count('lessons')
    ).all()[:6]

    total_students = get_performance_cache().get('total_students_count')
    if total_students is None:
        total_students = CustomUser.objects.filter(role='student').count()
        get_performance_cache().set('total_students_count', total_students, 300)
    total_courses = get_performance_cache().get('total_courses_count')
    if total_courses is None:
        total_courses = Profession.objects.count()
        get_performance_cache().set('total_courses_count', total_courses, 300)
    total_lessons = get_performance_cache().get('total_lessons_count')
    if total_lessons is None:
        total_lessons = Lesson.objects.count()
        get_performance_cache().set('total_lessons_count', total_lessons, 300)

    # --- Öquvçi uçun kurslari ---
    my_enrollments = []
    dashboard_courses = []
    quick_courses = []
    if request.user.is_teacher:
        quick_courses = list(request.user.teaching_professions.all()[:3])
    elif request.user.is_admin:
        quick_courses = list(Profession.objects.all()[:3])
    if request.user.is_learner or request.user.is_admin:
        my_enrollments = request.user.enrollments.select_related('profession').prefetch_related(
            models.Prefetch('profession__teachers', queryset=CustomUser.objects.order_by('pk'),
                            to_attr='dashboard_teachers')
        ).all()[:4]
        if my_enrollments:
            prof_ids = [e.profession_id for e in my_enrollments]
            
            # Bulk: barça kurslar uçun total countlar
            video_counts = dict(VideoLesson.objects.filter(
                lesson__profession_id__in=prof_ids
            ).values_list('lesson__profession_id').annotate(c=Count('id')).values_list('lesson__profession_id', 'c'))
            test_counts = dict(Lesson.objects.filter(
                profession_id__in=prof_ids, lesson_type='test'
            ).values_list('profession_id').annotate(c=Count('id')).values_list('profession_id', 'c'))
            hw_counts = dict(Homework.objects.filter(
                lesson__profession_id__in=prof_ids
            ).values_list('lesson__profession_id').annotate(c=Count('id')).values_list('lesson__profession_id', 'c'))
            
            # Bulk: foydalanuvçi progressi
            watched_counts = dict(VideoProgress.objects.filter(
                user=request.user, watched=True,
                video__lesson__profession_id__in=prof_ids
            ).values_list('video__lesson__profession_id').annotate(c=Count('id')).values_list('video__lesson__profession_id', 'c'))
            passed_counts = _passed_test_lesson_counts(request.user, prof_ids)
            done_hw_counts = dict(HomeworkSubmission.objects.filter(
                student=request.user,
                homework__lesson__profession_id__in=prof_ids,
                status__in=['accepted', 'graded']
            ).values_list('homework__lesson__profession_id').annotate(c=Count('homework_id', distinct=True)).values_list('homework__lesson__profession_id', 'c'))
            
            for enrollment in my_enrollments:
                pid = enrollment.profession_id
                prof = enrollment.profession
                total_items = video_counts.get(pid, 0) + test_counts.get(pid, 0) + hw_counts.get(pid, 0)
                if total_items == 0:
                    progress = 0
                else:
                    completed = watched_counts.get(pid, 0) + passed_counts.get(pid, 0) + done_hw_counts.get(pid, 0)
                    progress = min(100, round((completed / total_items) * 100))
                try:
                    teacher = next(iter(prof.dashboard_teachers), None)
                    teacher_name = teacher.get_full_name() if teacher else ''
                except Exception:
                    teacher_name = ''
                try:
                    photo_url = prof.photo.url if prof.photo else ''
                except Exception:
                    photo_url = ''
                try:
                    course_url = reverse('profession_detail', args=[pid])
                except Exception:
                    course_url = '#'
                dashboard_courses.append({
                    'id': pid,
                    'name': prof.name,
                    'progress': progress,
                    'teacher': teacher_name,
                    'photo': photo_url,
                    'is_paid': getattr(prof, 'is_paid', False),
                    'url': course_url,
                    'lesson_count': video_counts.get(pid, 0),
                })

    # Mondly-uslub dashboard uçun progress guruhlari
    db_completed = sum(1 for c in dashboard_courses if c['progress'] >= 100)
    db_inprogress = sum(1 for c in dashboard_courses if 0 < c['progress'] < 100)
    db_upcoming = sum(1 for c in dashboard_courses if c['progress'] == 0)
    db_avg_progress = round(
        sum(c['progress'] for c in dashboard_courses) / len(dashboard_courses)
    ) if dashboard_courses else 0
    db_featured = max(dashboard_courses, key=lambda c: c['progress']) if dashboard_courses else None

    # ⚠️ MUHIM:
    # unread_messages_count BU YERDA HISOBLANMAYDI!
    # Navbar uçun qiymat context processor orqali keladi.

    # Telegram ulaş tavsiyasi
    show_telegram_prompt = not request.user.telegram_id

    # Foydalanuvçi reytingdagi örni (coinlar böyiça)
    user_rank = None
    if request.user.is_student:
        user_rank = CustomUser.objects.filter(
            role='student', coins__gt=request.user.coins
        ).count() + 1

    # Top 5 öquvçi (coinlar böyiça)
    top_students_qs = CustomUser.objects.filter(
        role='student'
    ).order_by('-coins')[:5]
    top_students = []
    for i, s in enumerate(top_students_qs):
        try:
            s_photo = s.photo.url if s.photo else None
        except Exception:
            s_photo = None
        top_students.append({
            'rank': i + 1,
            'is_me': s.pk == request.user.pk,
            'photo': s_photo,
            'name': s.get_full_name() or s.username,
            'points': s.coins,
        })

    # =========================================================================
    # PREMIUM DASHBOARD — yangi kontekst kalitlari (faqat öquvchi/admin uçun)
    # Barçasi MAVJUD modellardan, himoyalangan (defensive) tarzda hisoblanadi.
    # =========================================================================
    user = request.user
    today = timezone.localdate()

    # ---- 1) today_recommendation -------------------------------------------
    today_recommendation = None
    try:
        in_progress = [c for c in dashboard_courses if 0 < c.get('progress', 0) < 100]
        chosen = None
        if in_progress:
            chosen = max(in_progress, key=lambda c: c.get('progress', 0))
        elif dashboard_courses:
            # most recent enrollment -> my_enrollments[0] (enrolled_at desc emas,
            # lekin dashboard_courses my_enrollments tartibida; birinçisini olamiz)
            chosen = dashboard_courses[0]
        if chosen:
            prog = chosen.get('progress', 0) or 0
            # total_lessons -> şu kursdagi darslar soni (himoyalangan)
            try:
                total_l = Lesson.objects.filter(profession_id=chosen['id']).count()
            except Exception:
                total_l = 0
            lessons_left = max(0, round(total_l * (100 - prog) / 100)) if total_l else 0
            try:
                rec_url = reverse('profession_detail', args=[chosen['id']])
            except Exception:
                rec_url = '#'
            today_recommendation = {
                'course_id': chosen['id'],
                'course_name': chosen.get('name', ''),
                'lessons_left': lessons_left,
                'total_lessons': total_l,
                'progress': prog,
                'url': rec_url,
            }
    except Exception:
        today_recommendation = None

    # ---- 2) daily_missions -------------------------------------------------
    daily_missions = []
    missions_done_count = 0
    missions_xp_earned = 0
    try:
        videos_today = VideoProgress.objects.filter(
            user=user, watched=True, watched_at__date=today
        ).count()
        quizzes_today = TestResult.objects.filter(
            student=user, passed=True, completed_at__date=today
        ).count()
        hw_today = HomeworkSubmission.objects.filter(
            student=user, submitted_at__date=today
        ).count()
        # coinlar böyiça bugungi faollik — agar derivable bölsa
        coins_today = None
        try:
            from coin.models import CoinTransaction as _CoinTx
            agg = _CoinTx.objects.filter(
                user=user, created_at__date=today, amount__gt=0
            ).aggregate(s=Sum('amount'))
            coins_today = agg.get('s') or 0
        except Exception:
            coins_today = None
        if coins_today is None:
            coins_today = min(20, (videos_today + quizzes_today + hw_today) * 10)

        _missions_raw = [
            {'key': 'watch_video', 'title': "Video dars köriş", 'icon': 'play-circle',
             'target': 1, 'current': videos_today, 'reward_xp': 10},
            {'key': 'pass_quiz', 'title': "Testdan ötiş", 'icon': 'patch-check',
             'target': 1, 'current': quizzes_today, 'reward_xp': 15},
            {'key': 'submit_homework', 'title': "Uy vazifa topşiriş", 'icon': 'journal-check',
             'target': 1, 'current': hw_today, 'reward_xp': 20},
            {'key': 'earn_coins', 'title': "Tanga işlaş", 'icon': 'coin',
             'target': 20, 'current': coins_today, 'reward_xp': 25},
        ]
        for m in _missions_raw:
            tgt = m['target'] or 1
            cur = m['current'] or 0
            done = cur >= tgt
            pct = min(100, round(cur / tgt * 100)) if tgt else 0
            m['done'] = done
            m['pct'] = pct
            if done:
                missions_done_count += 1
                missions_xp_earned += m['reward_xp']
            daily_missions.append(m)
    except Exception:
        daily_missions = []
        missions_done_count = 0
        missions_xp_earned = 0

    # ---- 3) learning_stats + weekly_spark ----------------------------------
    # Each metric is computed independently so a failure in one can never zero
    # the others (the previous single try/except zeroed ALL stats whenever the
    # weekly-spark date/datetime comparison threw).
    def _safe(fn, default=0):
        try:
            return fn()
        except Exception:
            return default

    lessons_completed = _safe(lambda: VideoProgress.objects.filter(user=user, watched=True).count())
    courses_completed = _safe(lambda: sum(1 for c in dashboard_courses if c.get('progress', 0) >= 100))
    study_hours = _safe(lambda: round((user.total_online_time or 0) / 60))
    weekly_minutes = _safe(lambda: (user.total_online_time or 0))
    quiz_accuracy = _safe(lambda: round(TestResult.objects.filter(student=user).aggregate(a=Avg('score')).get('a') or 0))
    certificates_earned = _safe(lambda: Certificate.objects.filter(student=user).count())

    # weekly_spark — joriy hafta (Du..Ya) faollik soni
    weekly_spark = [0] * 7
    try:
        week_start = today - timedelta(days=today.weekday())  # Monday (date)
        week_end = week_start + timedelta(days=6)
        spark = [0] * 7

        def _add_spark(dates_iter):
            for d in dates_iter:
                if not d:
                    continue
                dd = d.date() if hasattr(d, 'date') else d  # datetime -> date
                if week_start <= dd <= week_end:
                    spark[dd.weekday()] += 1

        _add_spark(VideoProgress.objects.filter(
            user=user, watched=True, watched_at__date__range=(week_start, week_end)
        ).values_list('watched_at', flat=True).iterator())
        _add_spark(TestResult.objects.filter(
            student=user, completed_at__date__range=(week_start, week_end)
        ).values_list('completed_at', flat=True).iterator())
        _add_spark(HomeworkSubmission.objects.filter(
            student=user, submitted_at__date__range=(week_start, week_end)
        ).values_list('submitted_at', flat=True).iterator())
        weekly_spark = spark
    except Exception:
        weekly_spark = [0] * 7
    weekly_spark_total = sum(weekly_spark)

    learning_stats = {
        'courses_completed': courses_completed,
        'lessons_completed': lessons_completed,
        'study_hours': study_hours,
        'weekly_minutes': weekly_minutes,
        'quiz_accuracy': quiz_accuracy,
        'certificates_earned': certificates_earned,
    }

    # ---- 4) heatmap_days (oxirgi 90 kun, har manba uçun 1 ta query) -------
    heatmap_days = []
    heatmap_max = 0
    heatmap_total = 0
    heatmap_active_days = 0
    try:
        start_date = today - timedelta(days=89)
        counts_by_date = {}

        def _accumulate(qs_dates):
            for dt in qs_dates:
                if not dt:
                    continue
                d = dt.date() if hasattr(dt, 'date') else dt
                if start_date <= d <= today:
                    counts_by_date[d] = counts_by_date.get(d, 0) + 1

        _accumulate(VideoProgress.objects.filter(
            user=user, watched=True, watched_at__date__gte=start_date
        ).values_list('watched_at', flat=True).iterator())
        _accumulate(TestResult.objects.filter(
            student=user, completed_at__date__gte=start_date
        ).values_list('completed_at', flat=True).iterator())
        _accumulate(HomeworkSubmission.objects.filter(
            student=user, submitted_at__date__gte=start_date
        ).values_list('submitted_at', flat=True).iterator())

        def _level(c):
            if c <= 0:
                return 0
            if c == 1:
                return 1
            if c <= 3:
                return 2
            if c <= 6:
                return 3
            return 4

        for i in range(90):
            d = start_date + timedelta(days=i)
            c = counts_by_date.get(d, 0)
            heatmap_total += c
            if c > 0:
                heatmap_active_days += 1
            if c > heatmap_max:
                heatmap_max = c
            heatmap_days.append({
                'date': d.strftime('%Y-%m-%d'),
                'count': c,
                'level': _level(c),
            })
    except Exception:
        heatmap_days = [{'date': (today - timedelta(days=89 - i)).strftime('%Y-%m-%d'),
                         'count': 0, 'level': 0} for i in range(90)]
        heatmap_max = 0
        heatmap_total = 0
        heatmap_active_days = 0

    # ---- 5) achievements ---------------------------------------------------
    achievements = []
    achievements_earned_count = 0
    try:
        try:
            passed_tests_count = TestResult.objects.filter(student=user, passed=True).count()
        except Exception:
            passed_tests_count = 0
        try:
            enroll_count = user.enrollments.count()
        except Exception:
            enroll_count = 0
        coins_val = user.coins or 0

        _ach_rules = [
            ('first_course', 'Birinçi kurs', 'mortarboard',
             'Birinçi kursga yozildingiz', enroll_count > 0),
            ('streak_7', '7 kunlik streak', 'fire',
             '7 kun ketma-ket faollik', (user.streak_days or 0) >= 7),
            ('streak_30', '30 kunlik streak', 'lightning-charge',
             '30 kun ketma-ket faollik', (user.streak_days or 0) >= 30),
            ('quiz_master', 'Test ustasi', 'patch-check',
             '5 ta testdan muvaffaqiyatli ötdingiz', passed_tests_count >= 5),
            ('centurion', 'Sentaurion', 'coin',
             '100 ta tanga töpladingiz', coins_val >= 100),
            ('scholar', 'Bilimdon', 'book',
             '20 ta dars tugatdingiz', lessons_completed >= 20),
            ('certified', 'Sertifikatli', 'award',
             'Sertifikat oldingiz', certificates_earned >= 1),
            ('premium', 'Premium', 'gem',
             'Premium foydalanuvçi', bool(getattr(user, 'is_premium', False))),
            ('top_10', 'Top 10', 'trophy',
             'Reytingda top 10 talikdasiz', bool(user_rank) and user_rank <= 10),
            ('explorer', 'Kaşfiyotçi', 'compass',
             '3 va undan ortiq kursga yozildingiz', enroll_count >= 3),
        ]
        for key, title, icon, desc, earned in _ach_rules:
            earned = bool(earned)
            if earned:
                achievements_earned_count += 1
            achievements.append({
                'key': key, 'title': title, 'icon': icon, 'desc': desc,
                'earned': earned,
                'earned_label': 'Olingan' if earned else 'Yopiq',
            })
    except Exception:
        achievements = []
        achievements_earned_count = 0
    achievements_total = len(achievements)

    # ---- 6) ai_recommendations --------------------------------------------
    ai_recommendations = {'course': None, 'article': None, 'practice': None}
    try:
        enrolled_ids = set(user.enrollments.values_list('profession_id', flat=True))
    except Exception:
        enrolled_ids = set()
    # course
    try:
        rec_prof = Profession.objects.filter(
            is_blocked=False
        ).exclude(id__in=enrolled_ids).first()
        if rec_prof:
            ai_recommendations['course'] = {
                'title': rec_prof.name,
                'reason': 'Sizning qiziqişlaringizga mos yangi kurs',
                'url': reverse('profession_detail', args=[rec_prof.id]),
                'icon': 'mortarboard',
            }
    except Exception:
        ai_recommendations['course'] = None
    # article
    try:
        from blog.models import Post as _BlogPost
        latest_post = _BlogPost.objects.filter(is_published=True).order_by('-created_at').first()
        if latest_post:
            try:
                art_url = reverse('blog:post_list')
            except Exception:
                art_url = '#'
            ai_recommendations['article'] = {
                'title': latest_post.title,
                'reason': 'Yangi maqola — bilimingizni kengaytiring',
                'url': art_url,
                'icon': 'newspaper',
            }
    except Exception:
        ai_recommendations['article'] = None
    # practice
    try:
        ai_recommendations['practice'] = {
            'title': 'Bilimni mustahkamlang',
            'reason': "Öyinlar orqali könikmangizni oşiring",
            'url': reverse('games:games_list'),
            'icon': 'controller',
        }
    except Exception:
        ai_recommendations['practice'] = None

    # ---- 7) recommended_courses (6 tagaça) --------------------------------
    recommended_courses = []
    try:
        rec_qs = Profession.objects.filter(
            is_blocked=False
        ).exclude(id__in=enrolled_ids).prefetch_related('teachers').annotate(
            lesson_count=Count('lessons')
        )[:6]
        for p in rec_qs:
            try:
                photo = p.photo.url if p.photo else ''
            except Exception:
                photo = ''
            try:
                t = p.teachers.first()
                t_name = t.get_full_name() if t else ''
            except Exception:
                t_name = ''
            try:
                rc_url = reverse('profession_detail', args=[p.id])
            except Exception:
                rc_url = '#'
            recommended_courses.append({
                'id': p.id,
                'name': p.name,
                'photo': photo,
                'teacher': t_name,
                'is_paid': getattr(p, 'is_paid', False),
                'lesson_count': getattr(p, 'lesson_count', 0) or 0,
                'reason': 'Sizning qiziqişlaringizga mos',
                'url': rc_url,
            })
    except Exception:
        recommended_courses = []

    # ---- 8) upcoming_events ------------------------------------------------
    upcoming_events = []
    try:
        from games.models import Competition as _Competition
        _now = timezone.now()
        _months = ['Yan', 'Fev', 'Mar', 'Apr', 'May', 'Iyn',
                   'Iyl', 'Avg', 'Sen', 'Okt', 'Noy', 'Dek']
        comp_qs = _Competition.objects.filter(
            is_active=True, end_time__gte=_now
        ).filter(
            _Competition.visible_q(request.user)
        ).order_by('start_time').distinct()[:5]
        for comp in comp_qs:
            st = timezone.localtime(comp.start_time) if comp.start_time else None
            if st:
                dt_label = st.strftime('%d.%m.%Y %H:%M')
                day = st.strftime('%d')
                month = _months[st.month - 1]
            else:
                dt_label = ''
                day = ''
                month = ''
            try:
                ev_url = reverse('games:competition_leaderboard', args=[comp.id])
            except Exception:
                ev_url = reverse('games:games_list')
            upcoming_events.append({
                'title': comp.title,
                'type': 'competition',
                'datetime_label': dt_label,
                'day': day,
                'month': month,
                'iso': st.isoformat() if st else '',
                'url': ev_url,
                'icon': 'trophy',
            })
    except Exception:
        upcoming_events = []

    # ---- 9) announcements --------------------------------------------------
    announcements = []
    announcements_unread = 0
    try:
        from blog.models import Post as _BlogPost2
        recent_cutoff = timezone.now() - timedelta(days=7)
        try:
            ann_url = reverse('blog:post_list')
        except Exception:
            ann_url = '#'
        for post in _BlogPost2.objects.filter(is_published=True).order_by('-created_at')[:4]:
            is_unread = bool(post.created_at and post.created_at >= recent_cutoff)
            if is_unread:
                announcements_unread += 1
            try:
                date_label = timezone.localtime(post.created_at).strftime('%d.%m.%Y') if post.created_at else ''
            except Exception:
                date_label = ''
            body_txt = (getattr(post, 'content', '') or '')
            announcements.append({
                'title': post.title,
                'body': body_txt[:160],
                'date_label': date_label,
                'is_unread': is_unread,
                'badge': 'new' if is_unread else 'update',
                'url': ann_url,
            })
    except Exception:
        announcements = []
        announcements_unread = 0

    # ---- 10) quick_access --------------------------------------------------
    quick_access = []
    _qa_items = [
        ('Kurslar', 'professions', None, 'mortarboard', '#6366f1'),
        ('AI Çat', 'ai_yordamchi:chat_home', None, 'robot', '#8b5cf6'),
        ("Öyinlar", 'games:games_list', None, 'controller', '#ec4899'),
        ('Market', 'coin:market_list', None, 'bag', '#f59e0b'),
        ('Blog', 'blog:post_list', None, 'newspaper', '#10b981'),
        ('Sertifikatlar', 'my_certificates', None, 'award', '#06b6d4'),
        ('Reyting', 'leaderboard', None, 'bar-chart', '#ef4444'),
        ('Coding Arena', 'games:live_games', None, 'cpu', '#3b82f6'),
    ]
    for label, url_name, args, icon, color in _qa_items:
        try:
            url = reverse(url_name, args=args) if args else reverse(url_name)
        except Exception:
            continue
        quick_access.append({
            'label': label,
            'url': url,
            'icon': icon,
            'color': color,
        })

    # --- Template uçun tayyor StatsCard röyxati (learning_stats dan) ---
    _ls = learning_stats if isinstance(learning_stats, dict) else {}
    stat_cards = [
        {'label': "Tugatilgan kurslar", 'value': _ls.get('courses_completed', 0), 'icon': 'mortarboard-fill', 'color': 'accent'},
        {'label': "Tugatilgan darslar", 'value': _ls.get('lessons_completed', 0), 'icon': 'collection-play-fill', 'color': 'accent2'},
        {'label': "Jami öqiş (soat)", 'value': _ls.get('study_hours', 0), 'icon': 'clock-history', 'color': 'success'},
        {'label': "Online vaqt (daqiqa)", 'value': _ls.get('weekly_minutes', 0), 'icon': 'clock', 'color': 'warning'},
        {'label': "Test aniqligi", 'value': str(_ls.get('quiz_accuracy', 0)) + '%', 'icon': 'bullseye', 'color': 'accent'},
        {'label': "Sertifikatlar", 'value': _ls.get('certificates_earned', 0), 'icon': 'patch-check-fill', 'color': 'success'},
    ]

    # --- Kunlik vazifalar sarlavhasi uçun matn ---
    missions_subtitle = "%s / 4 bajarildi · +%s XP" % (missions_done_count, missions_xp_earned)

    # --- Foydalanuvçining reyting qatori (top 5 içida bölmasa) ---
    my_leader_row = None
    try:
        if user_rank and user_rank > 5 and request.user.is_student:
            try:
                _my_photo = request.user.photo.url if request.user.photo else None
            except Exception:
                _my_photo = None
            my_leader_row = {
                'rank': user_rank,
                'is_me': True,
                'photo': _my_photo,
                'name': request.user.get_full_name() or request.user.username,
                'points': request.user.coins or 0,
            }
    except Exception:
        my_leader_row = None

    # --- Daraja / XP (100 coin = 1 daraja) ---
    _coins = user.coins or 0
    level_number = _coins // 100 + 1
    xp_in_level = _coins % 100
    xp_to_next = 100 - xp_in_level

    # --- Vaqtga qarab salomlaşuv (server local time = öquvçi vaqti) ---
    _hour = timezone.localtime().hour
    if _hour < 6:
        greeting, greeting_icon = 'Xayrli tun', '🌙'
    elif _hour < 12:
        greeting, greeting_icon = 'Xayrli tong', '☀️'
    elif _hour < 18:
        greeting, greeting_icon = 'Xayrli kun', '👋'
    else:
        greeting, greeting_icon = 'Xayrli keç', '🌆'

    # --- Kun iqtibosi, keyingi jadval darsi, kun savoli ---
    daily_quote = _daily_quote(today)
    next_lesson = _next_study_lesson(user)

    daily_question = None
    daily_question_state = 'none'
    try:
        _dq = _daily_question_for(user, today)
        if _dq:
            _log = _daily_question_log(user, today)
            if _log:
                daily_question_state = 'correct' if _DQ_OK in _log.description else 'wrong'
            else:
                daily_question_state = 'new'
            # töğri javob ID'si şablonga faqat javob berilgandan keyin uzatiladi
            daily_question = {k: v for k, v in _dq.items() if k != 'correct_id'}
            if daily_question_state != 'new':
                daily_question['correct_id'] = _dq['correct_id']
    except Exception:
        daily_question = None
        daily_question_state = 'none'

    context = {
        'professions': professions,
        'total_students': total_students,
        'stat_cards': stat_cards,
        'missions_subtitle': missions_subtitle,
        'my_leader_row': my_leader_row,
        'total_courses': total_courses,
        'total_lessons': total_lessons,
        'my_enrollments': my_enrollments,
        'dashboard_courses': dashboard_courses,
        'quick_courses': quick_courses,
        'show_telegram_prompt': show_telegram_prompt,
        'user_rank': user_rank,
        'top_students': top_students,
        'db_completed': db_completed,
        'db_inprogress': db_inprogress,
        'db_upcoming': db_upcoming,
        'db_avg_progress': db_avg_progress,
        'db_featured': db_featured,
        # --- YANGI premium dashboard kalitlari ---
        'today_recommendation': today_recommendation,
        'daily_missions': daily_missions,
        'missions_done_count': missions_done_count,
        'missions_total': 4,
        'missions_xp_earned': missions_xp_earned,
        'learning_stats': learning_stats,
        'weekly_spark': weekly_spark,
        'weekly_spark_total': weekly_spark_total,
        'heatmap_days': heatmap_days,
        'heatmap_max': heatmap_max,
        'heatmap_total': heatmap_total,
        'heatmap_active_days': heatmap_active_days,
        'achievements': achievements,
        'achievements_earned_count': achievements_earned_count,
        'achievements_total': achievements_total,
        'ai_recommendations': ai_recommendations,
        'recommended_courses': recommended_courses,
        'upcoming_events': upcoming_events,
        'announcements': announcements,
        'announcements_unread': announcements_unread,
        'quick_access': quick_access,
        # --- Redesign: daraja, iqtibos, keyingi dars, kun savoli ---
        'level_number': level_number,
        'xp_in_level': xp_in_level,
        'xp_to_next': xp_to_next,
        'greeting': greeting,
        'greeting_icon': greeting_icon,
        'today_weekday': today.weekday(),
        'daily_quote': daily_quote,
        'next_lesson': next_lesson,
        'daily_question': daily_question,
        'daily_question_state': daily_question_state,
    }
    from .course_pricing import discounted_courses
    from coin.topup_pricing import promos
    from kurslarim.access import purchased_course_ids
    context['sale_promos'] = discounted_courses(exclude_ids=purchased_course_ids(user))
    context['coin_promos'] = promos()

    # Premium reklamasi: premiumga — tanga 50% arzon, boşqalarga — premium taklifi.
    from coin.topup_pricing import PREMIUM_DISCOUNT_PERCENT
    from .models import CoinPricingSettings
    # Misol paket: 10 000 tanga (narx 1 söm bölsa ham farq körinadi).
    gross = 10000 * CoinPricingSettings.current().som_per_coin
    context['premium_coin_offer'] = {
        'percent': PREMIUM_DISCOUNT_PERCENT,
        'coins': 10000,
        'gross': gross,
        'payable': (gross * (100 - PREMIUM_DISCOUNT_PERCENT) + 99) // 100,
    }

    # "Bilasizmi?" — har soatda va har kirişda yangi savol, javobini AI beradi.
    from . import curiosity
    context['curiosity_question'] = curiosity.pick_question(user)
    curiosity.remember(request, context['curiosity_question'])
    curiosity.refresh_if_stale()

    # Birinçi kirişdagi qöllanma (yoki ?tour=1 — qayta köriş)
    context['show_student_tour'] = user.is_learner and (
        not user.onboarding_done or request.GET.get('tour') == '1'
    )

    return render(request, 'accounts/home.html', context)


@login_required
def global_search(request):
    """Premium universal qidiruv — kurslar, darslar, market, kitoblar, blog + tavsiyalar."""
    from django.urls import reverse
    from django.db.models import Q as _Q, Count as _Count
    from coin.models import Product
    from blog.models import Post

    q = (request.GET.get('q') or '').strip()
    groups = []
    total = 0

    enrolled_ids = set()
    try:
        if request.user.is_authenticated:
            enrolled_ids = set(request.user.enrollments.values_list('profession_id', flat=True))
    except Exception:
        enrolled_ids = set()

    def course_dict(p):
        try:
            img = p.photo.url if p.photo else ''
        except Exception:
            img = ''
        teacher = None
        try:
            teacher = p.teachers.first()
        except Exception:
            teacher = None
        t_name = teacher.get_full_name() if teacher else 'IT Creative'
        try:
            t_photo = teacher.photo.url if (teacher and teacher.photo) else ''
        except Exception:
            t_photo = ''
        lessons = getattr(p, '_lessons', 0) or 0
        students = getattr(p, '_students', 0) or 0
        hours = max(1, round(lessons * 8 / 60)) if lessons else 0
        if lessons < 12:
            level, level_lab = 'beginner', "Boşlanğiç"
        elif lessons < 30:
            level, level_lab = 'inter', "Örta"
        else:
            level, level_lab = 'advanced', 'Yuqori'
        dur = 'short' if hours < 2 else ('mid' if hours <= 6 else 'long')
        return {
            'level': level, 'level_lab': level_lab, 'dur': dur,
            'title': p.name,
            'url': reverse('profession_detail', args=[p.pk]),
            'image': img,
            'teacher': t_name,
            'teacher_photo': t_photo,
            'students': students,
            'lessons': lessons,
            'hours': hours,
            'rating': round(4.5 + (p.pk % 5) * 0.1, 1),
            'is_paid': getattr(p, 'is_paid', False),
            'enrolled': p.pk in enrolled_ids,
        }

    if q:
        # --- Kurslar ---
        prof_qs = Profession.objects.filter(
            _Q(name__icontains=q) | _Q(description__icontains=q)
        ).annotate(
            _lessons=_Count('lessons', distinct=True),
            _students=_Count('enrollments', distinct=True),
        ).prefetch_related('teachers')[:12]
        course_items = [course_dict(p) for p in prof_qs]
        if course_items:
            groups.append({'key': 'course', 'kind': 'course', 'label': 'Kurslar',
                           'icon': 'bi-mortarboard-fill', 'color': '#3B82F6', 'items': course_items})
            total += len(course_items)

        # --- Darslar ---
        lesson_qs = Lesson.objects.select_related('profession').filter(title__icontains=q)[:10]
        lesson_items = [{
            'title': l.title,
            'subtitle': l.profession.name if l.profession_id else '',
            'url': reverse('lesson_view', args=[l.pk]),
            'image': '',
        } for l in lesson_qs]
        if lesson_items:
            groups.append({'key': 'lesson', 'kind': 'simple', 'label': 'Darslar',
                           'icon': 'bi-play-circle-fill', 'color': '#8B5CF6', 'items': lesson_items})
            total += len(lesson_items)

        # --- Market ---
        prod_qs = Product.visible_to(request.user).filter(
            _Q(name__icontains=q) | _Q(description__icontains=q)
        )[:10]
        prod_items = []
        for pr in prod_qs:
            try:
                img = pr.image.url if pr.image else ''
            except Exception:
                img = ''
            prod_items.append({'title': pr.name, 'subtitle': f"{pr.coin_price} coin",
                               'url': reverse('coin:market_detail', args=[pr.token]), 'image': img})
        if prod_items:
            groups.append({'key': 'market', 'kind': 'simple', 'label': 'Market',
                           'icon': 'bi-bag-heart-fill', 'color': '#22C55E', 'items': prod_items})
            total += len(prod_items)

        # --- Kitoblar ---
        book_qs = Book.objects.filter(_Q(title__icontains=q) | _Q(author__icontains=q))[:10]
        book_items = []
        for b in book_qs:
            try:
                img = b.cover_image.url if b.cover_image else ''
            except Exception:
                img = ''
            book_items.append({'title': b.title, 'subtitle': b.author or '',
                               'url': reverse('my_book_rentals'), 'image': img})
        if book_items:
            groups.append({'key': 'book', 'kind': 'simple', 'label': 'Kitoblar',
                           'icon': 'bi-book-half', 'color': '#F59E0B', 'items': book_items})
            total += len(book_items)

        # --- Blog ---
        post_qs = Post.objects.filter(
            _Q(title__icontains=q) | _Q(content__icontains=q), is_published=True
        )[:10]
        post_items = []
        for po in post_qs:
            try:
                img = po.image.url if po.image else ''
            except Exception:
                img = ''
            post_items.append({'title': po.title, 'subtitle': (po.content or '')[:90],
                               'url': reverse('blog:post_detail', args=[po.token]), 'image': img})
        if post_items:
            groups.append({'key': 'blog', 'kind': 'simple', 'label': 'Blog',
                           'icon': 'bi-newspaper', 'color': '#EC4899', 'items': post_items})
            total += len(post_items)

    # --- Tavsiya etilgan kurslar (empty state + pastki blok uçun) ---
    rec_qs = Profession.objects.filter(is_blocked=False).annotate(
        _lessons=_Count('lessons', distinct=True),
        _students=_Count('enrollments', distinct=True),
    ).prefetch_related('teachers').order_by('-_students')[:8]
    recommended = [course_dict(p) for p in rec_qs]

    return render(request, 'accounts/search.html', {
        'q': q, 'groups': groups, 'total': total, 'recommended': recommended,
    })


@login_required
def professions_view(request):
    # Kartada körinadigan tarkib: neçta bölim, test va vazifa bor
    professions = Profession.objects.prefetch_related('teachers').annotate(
        section_total=Count('sections', filter=Q(sections__is_active=True), distinct=True),
        test_total=Count('lessons', filter=Q(lessons__lesson_type='test'), distinct=True),
        homework_total=Count('lessons', filter=Q(lessons__lesson_type='homework'), distinct=True),
    )
    enrolled_ids = []
    if request.user.is_authenticated and request.user.is_learner:
        enrolled_ids = list(request.user.enrollments.values_list('profession_id', flat=True))
        # Sort: enrolled courses first, then the rest
        professions = sorted(professions, key=lambda p: (p.pk not in enrolled_ids, p.pk))

        # Har bir kurs uçun progress hisoblaş (bulk querylar)
        if enrolled_ids:
            video_counts = dict(VideoLesson.objects.filter(
                lesson__profession_id__in=enrolled_ids
            ).values_list('lesson__profession_id').annotate(c=Count('id')).values_list('lesson__profession_id', 'c'))
            test_counts = dict(Lesson.objects.filter(
                profession_id__in=enrolled_ids, lesson_type='test'
            ).values_list('profession_id').annotate(c=Count('id')).values_list('profession_id', 'c'))
            hw_counts = dict(Homework.objects.filter(
                lesson__profession_id__in=enrolled_ids
            ).values_list('lesson__profession_id').annotate(c=Count('id')).values_list('lesson__profession_id', 'c'))
            watched_counts = dict(VideoProgress.objects.filter(
                user=request.user, watched=True, video__lesson__profession_id__in=enrolled_ids
            ).values_list('video__lesson__profession_id').annotate(c=Count('id')).values_list('video__lesson__profession_id', 'c'))
            passed_counts = _passed_test_lesson_counts(request.user, enrolled_ids)
            done_hw_counts = dict(HomeworkSubmission.objects.filter(
                student=request.user, homework__lesson__profession_id__in=enrolled_ids, status__in=['accepted', 'graded']
            ).values_list('homework__lesson__profession_id').annotate(c=Count('homework_id', distinct=True)).values_list('homework__lesson__profession_id', 'c'))

            for p in professions:
                if p.pk not in enrolled_ids:
                    p.progress = None
                    continue
                total_items = video_counts.get(p.pk, 0) + test_counts.get(p.pk, 0) + hw_counts.get(p.pk, 0)
                if total_items == 0:
                    p.progress = 0
                    continue
                completed = watched_counts.get(p.pk, 0) + passed_counts.get(p.pk, 0) + done_hw_counts.get(p.pk, 0)
                p.progress = min(round((completed / total_items) * 100), 100)

    # Qidiruv, filtr va sahifalaş brauzerda barça kurslar böyiça işlaydi:
    # serverda sahifalaş qidiruvni faqat joriy 8 ta kurs bilan çeklab qöyardi.
    from .course_pricing import attach_prices
    professions = attach_prices(professions)

    # Öquvçilar uçun kurs tokenlarini yaratiş
    course_token_map = {}
    purchased_ids = set()
    if request.user.is_authenticated and request.user.is_learner:
        for p in professions:
            ct = StudentCourseToken.get_or_create(request.user, p)
            course_token_map[p.pk] = ct.token
        from kurslarim.access import purchased_course_ids
        purchased_ids = purchased_course_ids(request.user)

    # Bir martalik tölov bilan sotiladigan kurslar — alohida marketing bloki.
    sale_courses = [p for p in professions if p.is_sold_once and not p.is_blocked and not p.is_mandatory]

    # Birlashgan kurslar (masalan "Front End"): öquvçiga — har bir kurs progressi bilan.
    from .course_bundles import bundle_cards
    course_bundles = bundle_cards(request.user, set(enrolled_ids))

    return render(request, 'accounts/professions.html', {
        'professions': professions,
        'all_count': len(professions),
        'sale_courses': sale_courses,
        'course_bundles': course_bundles,
        'purchased_ids': purchased_ids,
        'enrolled_ids': enrolled_ids,
        'course_token_map': course_token_map,
    })


def _passed_test_lesson_counts(user, profession_ids):
    """Eski test va yangi Quiz natijalarini dars böyiça takrorlamasdan sanaydi."""
    passed_by_profession = {}

    legacy_rows = TestResult.objects.filter(
        student=user,
        passed=True,
        test__lesson__profession_id__in=profession_ids,
    ).values_list('test__lesson__profession_id', 'test__lesson_id')
    for profession_id, lesson_id in legacy_rows:
        passed_by_profession.setdefault(profession_id, set()).add(lesson_id)

    from quiz.models import Attempt as QuizAttempt
    quiz_rows = QuizAttempt.objects.filter(
        student=user,
        passed=True,
        status__in=[QuizAttempt.Status.SUBMITTED, QuizAttempt.Status.GRADED],
        quiz__lesson__profession_id__in=profession_ids,
    ).values_list('quiz__lesson__profession_id', 'quiz__lesson_id')
    for profession_id, lesson_id in quiz_rows:
        if lesson_id:
            passed_by_profession.setdefault(profession_id, set()).add(lesson_id)

    return {
        profession_id: len(lesson_ids)
        for profession_id, lesson_ids in passed_by_profession.items()
    }


def _homework_locked_lesson_ids(lessons, completed_lesson_ids, submitted_homework_lesson_ids=None):
    """
    Given an ordered sequence of lessons (one section's lessons, dictionary
    type already excluded), returns (locked_ids, blocking_lesson): a
    homework-type lesson that hasn't been approved yet (its latest
    HomeworkSubmission.status not in accepted/graded -- i.e. still pending
    review, or sent back with 'revision' by a teacher/admin/AI) blocks every
    lesson that comes *after* it in this sequence.

    This exists because section-level unlocking (section_started /
    section_completed, computed by each caller) only tracks whether the
    *section* has been started -- once true, it stays true forever, so a
    rejected or still-pending homework mid-section previously let a student
    click straight through to later lessons in the same section without
    ever resubmitting. This helper adds the missing per-lesson check on top
    of that section-level result; it does not decide whether the section
    itself is open.

    The homework lesson itself is never locked by this -- a student must
    still be able to open it to submit or resubmit.
    """
    # Vazifa TOPŞIRILGAÇ (baholanmasa ham) undan keyingi darslar — jumladan
    # test — oçiladi. Faqat umuman topşirilmagan vazifa keyingilarni bloklaydi.
    submitted_homework_lesson_ids = submitted_homework_lesson_ids or set()
    locked_ids = set()
    blocking_lesson = None
    for l in lessons:
        if blocking_lesson is not None:
            locked_ids.add(l.pk)
            continue
        if (l.lesson_type == 'homework'
                and l.pk not in completed_lesson_ids
                and l.pk not in submitted_homework_lesson_ids):
            blocking_lesson = l
    return locked_ids, blocking_lesson


def _build_course_roadmap(sections, section_completed=None, section_is_locked=None, section_daily_locked=None):
    """Kurs yöl xaritasi (roadmap) uçun node holatlari va umumiy progressni qaytaradi.

    Har bir node holati:
      - 'completed' : bölim töliq tugallangan
      - 'active'    : birinçi tugallanmagan va oçiq bölim (foydalanuvçi şu yerda)
      - 'locked'    : qulflangan yoki kunlik çeklov tufayli yopiq
      - 'upcoming'  : navbatdagi oçiq bölim (hali boşlanmagan)
    """
    section_completed = section_completed or {}
    section_is_locked = section_is_locked or {}
    section_daily_locked = section_daily_locked or {}

    nodes = []
    active_assigned = False
    completed_count = 0

    for idx, section in enumerate(sections):
        sid = section.id
        lessons_count = sum(1 for l in section.lessons.all() if l.lesson_type != 'dictionary')
        is_done = bool(section_completed.get(sid))
        is_locked = bool(section_is_locked.get(sid)) or bool(section_daily_locked.get(sid))

        if is_done:
            state = 'completed'
            completed_count += 1
        elif is_locked:
            state = 'locked'
        elif not active_assigned:
            state = 'active'
            active_assigned = True
        else:
            state = 'upcoming'

        nodes.append({
            'id': sid,
            'title': section.title,
            'num': idx + 1,
            'state': state,
            'lessons_count': lessons_count,
        })

    total = len(nodes)
    progress = round(completed_count * 100 / total) if total else 0
    active_index = next((n['num'] - 1 for n in nodes if n['state'] == 'active'), None)

    return {
        'nodes': nodes,
        'progress': progress,
        'completed_count': completed_count,
        'total': total,
        'active_index': active_index,
    }


def _final_test_info(lesson):
    """Yakuniy test kartoçkasi uçun vaqt va ötiş bali (eski Test yoki Quiz)."""
    test = getattr(lesson, 'test', None)
    if test is not None:
        return {'time_limit': test.time_limit, 'passing_score': test.passing_score}
    quiz = getattr(lesson, 'quiz', None)
    settings_ = getattr(quiz, 'settings', None) if quiz is not None else None
    return {
        'time_limit': settings_.time_limit_minutes if settings_ else 0,
        'passing_score': settings_.passing_score if settings_ else 0,
    }


@login_required
def profession_detail(request, pk):
    profession = get_object_or_404(Profession, pk=pk)

    # Öquvçilar uçun PK orqali kirişni bloklaş — token URL ga yönaltiriş
    if request.user.is_authenticated and request.user.is_learner and not request.user.is_admin:
        from .models import StudentCourseToken
        ct = StudentCourseToken.get_or_create(request.user, profession)
        return redirect('student_course_view', token=ct.token)

    is_enrolled = False
    enrollment_request = None
    unenroll_request = None
    
    # Bloklangan kursga kiriş çeklovi (faqat öquvçilar uçun)
    if profession.is_blocked and request.user.is_student:
        messages.warning(request, f"'{profession.name}' kursi vaqtinça bloklangan. {profession.blocked_reason or ''}")
        return redirect('professions')
    
    is_teaching = False
    if request.user.is_learner:
        is_enrolled = CourseEnrollment.objects.filter(user=request.user, profession=profession).exists() or request.user.profession == profession
        if is_enrolled:
            unenroll_request = CourseUnenrollRequest.objects.filter(user=request.user, profession=profession, status='pending').first()
        else:
            enrollment_request = CourseEnrollmentRequest.objects.filter(user=request.user, profession=profession).first()
    elif request.user.is_teacher:
        is_teaching = request.user.teaching_professions.filter(pk=profession.pk).exists()
    
    sections = []
    lessons_without_section = []
    lesson_progress = {}
    daily_section_today = None
    daily_limit_section_name = None
    section_is_locked = {}
    section_daily_locked = {}
    lesson_is_locked = {}
    daily_lesson_today = None
    
    token_map = {}
    if is_enrolled or is_teaching or request.user.is_admin:
        sections = profession.sections.filter(is_active=True).prefetch_related('lessons')
        lessons_without_section = profession.lessons.filter(section__isnull=True)
        
        if request.user.is_learner and is_enrolled:
            StudentLessonToken.ensure_for_profession(request.user, profession)
            token_map = {
                t.lesson_id: t
                for t in StudentLessonToken.objects.filter(
                    student=request.user, lesson__profession=profession
                )
            }
        
        # Get user progress for lessons
        if request.user.is_authenticated:
            # Video progress
            watched_videos = VideoProgress.objects.filter(
                user=request.user, 
                watched=True,
                video__lesson__profession=profession
            ).values_list('video__lesson_id', flat=True)
            
            # Test results - eng oxirgi natijani oliş
            test_results = TestResult.objects.filter(
                student=request.user,
                test__lesson__profession=profession
            ).order_by('test__lesson_id', '-completed_at')

            # Har bir test uçun eng oxirgi natijani saqlaş
            latest_test_results = {}
            for tr in test_results:
                if tr.test.lesson_id not in latest_test_results:
                    latest_test_results[tr.test.lesson_id] = tr

            # Quiz Attempt natijalari (legacy_test_id bölmagan yangi quizlar uçun)
            # TestResult yöq bölgan test darslari uçun Attempt dan foydalanamiz
            from quiz.models import Attempt as QuizAttempt
            quiz_attempts = QuizAttempt.objects.filter(
                student=request.user,
                quiz__lesson__profession=profession,
                status__in=[QuizAttempt.Status.SUBMITTED, QuizAttempt.Status.GRADED],
            ).select_related('quiz').order_by('quiz__lesson_id', '-started_at')
            latest_quiz_attempts = {}
            for qa in quiz_attempts:
                lid = qa.quiz.lesson_id
                if lid and lid not in latest_quiz_attempts:
                    latest_quiz_attempts[lid] = qa

            # Homework submissions - eng oxirgi topşiriqni oliş
            hw_submissions = HomeworkSubmission.objects.filter(
                student=request.user,
                homework__lesson__profession=profession
            ).order_by('homework__lesson_id', '-submitted_at')
            
            latest_hw_submissions = {}
            for hw in hw_submissions:
                if hw.homework.lesson_id not in latest_hw_submissions:
                    latest_hw_submissions[hw.homework.lesson_id] = hw
            # Topşirilgan vazifa darslari — test blokidan çiqariş uçun
            submitted_hw_lesson_ids = set(latest_hw_submissions.keys())

            for vid in watched_videos:
                lesson_progress[vid] = {'completed': True, 'type': 'video'}
            
            for lesson_id, tr in latest_test_results.items():
                lesson_progress[lesson_id] = {
                    'completed': tr.passed,
                    'type': 'test',
                    'passed': tr.passed,
                    'score': tr.score,
                    'attempted': True,
                }

            # Yangi quizlar uçun Attempt natijasini qöşiş (TestResult bölmagan darslar)
            for lesson_id, qa in latest_quiz_attempts.items():
                if lesson_id not in lesson_progress:
                    lesson_progress[lesson_id] = {
                        'completed': qa.passed,
                        'type': 'test',
                        'passed': qa.passed,
                        'score': qa.score,
                        'attempted': True,
                    }

            for lesson_id, hw in latest_hw_submissions.items():
                is_completed = hw.status in ('accepted', 'graded')
                lesson_progress[lesson_id] = {
                    'completed': is_completed,
                    'type': 'homework',
                    'graded': hw.grade is not None,
                    'status': hw.status,
                    'attempted': True,
                }
            
            # Speaking submissions
            speaking_submissions = SpeakingSubmission.objects.filter(
                student=request.user,
                speaking_task__lesson__profession=profession
            ).order_by('speaking_task__lesson_id', '-submitted_at')
            
            latest_speaking = {}
            for sp in speaking_submissions:
                if sp.speaking_task.lesson_id not in latest_speaking:
                    latest_speaking[sp.speaking_task.lesson_id] = sp
            
            for lesson_id, sp in latest_speaking.items():
                is_completed = sp.status in ('graded',)
                lesson_progress[lesson_id] = {
                    'completed': is_completed,
                    'type': 'speaking',
                    'graded': sp.grade is not None,
                    'status': sp.status,
                    'attempted': True,
                }
        
        # Kunlik bölim çeklovi ma'lumotlarini oliş
        daily_section_today = None
        daily_limit_section_name = None
        accessed_section_ids = set()
        if request.user.is_student and not request.user.is_admin and daily_limit_active(request.user, profession):
            from datetime import date
            today = date.today()
            # Oldin kirilgan barça bölimlar (har qanday kunda)
            accessed_section_ids = set(
                DailySectionAccess.objects.filter(
                    user=request.user, profession=profession
                ).values_list('section_id', flat=True)
            )
            today_access = DailySectionAccess.objects.filter(
                user=request.user, profession=profession, accessed_date=today
            ).select_related('section').first()
            if today_access:
                daily_section_today = today_access.section_id
                daily_limit_section_name = today_access.section.title

        section_is_locked = {}
        if request.user.is_student and not request.user.is_admin:
            pass  # section lock dars darajasidan kelib çiqadi — pastda hisoblanadi
        else:
            # Admin va öqituvçi uçun barça bölimlar oçiq
            for section in sections:
                section_is_locked[section.id] = False
            section_is_locked['no_section'] = False
            section_daily_locked = {s.id: False for s in sections}
    
    # ===== BÖLIM DARAJASIDA QULFLAŞ MANTIGI =====
    # Qoidalar:
    #   1. Tugatilgan bölimlar → har doim OÇIQ (takrorlaş uçun)
    #   2. Boşlangan bölim (kamida 1 dars bajarilgan) → OÇIQ (davom ettiriş)
    #   3. Yangi bölim (boşlanmagan, oldingi tugatilgan) → kunlik çeklaş
    #      - Bugun boşqa bölimga kirilgan bölsa → ERTAGA oçiladi
    #      - Kirilmagan bölsa → OÇIQ
    #   4. Qolgan bölimlar → QULFLANGAN (oldingi tugatilmagan)
    if request.user.is_student and not request.user.is_admin and (is_enrolled):
        from datetime import date
        today = date.today()
        completed_lesson_ids = set(
            lid for lid, prog in lesson_progress.items() if prog.get('completed')
        )

        ordered_sections = list(sections)

        # Har bir bölim holati
        section_completed = {}
        section_started = {}
        for section in ordered_sections:
            section_lessons = [l for l in section.lessons.all() if l.lesson_type != 'dictionary']
            if not section_lessons:
                section_completed[section.id] = True
                section_started[section.id] = True
            else:
                section_completed[section.id] = all(l.pk in completed_lesson_ids for l in section_lessons)
                section_started[section.id] = any(l.pk in completed_lesson_ids for l in section_lessons)

        # Bugungi DailySectionAccess
        today_section_access = None
        limit_on = daily_limit_active(request.user, profession)
        if limit_on:
            today_section_access = DailySectionAccess.objects.filter(
                user=request.user, profession=profession, accessed_date=today
            ).select_related('section').first()

        section_is_locked = {}
        section_daily_locked = {}
        for idx, section in enumerate(ordered_sections):
            if section_completed[section.id]:
                # 1. Tugatilgan → oçiq
                section_is_locked[section.id] = False
                section_daily_locked[section.id] = False
            elif section_started[section.id]:
                # 2. Boşlangan → oçiq
                section_is_locked[section.id] = False
                section_daily_locked[section.id] = False
            else:
                # 3-4. Boşlanmagan bölim
                prev_completed = (idx == 0) or section_completed[ordered_sections[idx - 1].id]

                if not prev_completed:
                    # 4. Oldingi tugatilmagan → qulflangan
                    section_is_locked[section.id] = True
                    section_daily_locked[section.id] = False
                else:
                    # 3. Oldingi tugatilgan → kunlik çeklaş
                    if limit_on and today_section_access and today_section_access.section_id != section.id:
                        # Bugun boşqa bölimga kirilgan → ertaga
                        section_is_locked[section.id] = False
                        section_daily_locked[section.id] = True
                    else:
                        # Bugun heç narsaga kirilmagan yoki aynan şu bölimga → oçiq
                        section_is_locked[section.id] = False
                        section_daily_locked[section.id] = False

        # Darslar holati — bölim holatiga qarab
        for section in ordered_sections:
            s_locked = section_is_locked.get(section.id)
            s_daily = section_daily_locked.get(section.id)
            section_lessons = [l for l in section.lessons.all() if l.lesson_type != 'dictionary']

            if s_locked:
                for l in section_lessons:
                    lesson_is_locked[l.pk] = 'sequential'
            elif s_daily:
                for l in section_lessons:
                    lesson_is_locked[l.pk] = 'daily'
            else:
                for l in section_lessons:
                    # Bölim oçilgan bölsa, uning video, vazifa va oddiy testlari
                    # birgalikda oçiladi. Keyingi bölim esa hammasi bajarilgaç oçiladi.
                    lesson_is_locked[l.pk] = False

        # Daily limit banner
        if today_section_access:
            daily_section_today = today_section_access.section_id
            daily_limit_section_name = today_section_access.section.title

        # Bölimsiz darslar
        if lessons_without_section:
            no_sec_lessons = [l for l in lessons_without_section if l.lesson_type != 'dictionary']
            has_open = any(lesson_is_locked.get(l.pk) is False or lesson_is_locked.get(l.pk) is None for l in no_sec_lessons)
            section_is_locked['no_section'] = not has_open
        else:
            section_is_locked['no_section'] = False

    # Kursdoşlar - şu kursga yozilgan öquvçilar
    classmates = []
    if is_enrolled or is_teaching or request.user.is_admin:
        classmates = CustomUser.objects.filter(
            enrollments__profession=profession,
            role='student'
        ).exclude(pk=request.user.pk).order_by('-last_activity', '-coins')[:20]
    
    course_teachers = profession.teachers.all()

    # Kurs tugatilganligini tekşiriş va sertifikat sörovi
    course_completed = False
    certificate = None
    if is_enrolled and request.user.is_student:
        # Barça bölimlardagi barça darslar (luğat bundan mustasno) tugatilganmi
        all_lessons = Lesson.objects.filter(profession=profession).exclude(lesson_type='dictionary')
        # Bölimlar mavjud lekin içida dars yöq bölsa — kurs tugallanmagan
        course_sections = profession.sections.filter(is_active=True)
        has_empty_section = any(
            not s.lessons.exists() for s in course_sections
        ) if course_sections.exists() else False
        if all_lessons.exists() and not has_empty_section:
            completed_ids = set(
                lid for lid, prog in lesson_progress.items() if prog.get('completed')
            )
            all_done = all(lesson.pk in completed_ids for lesson in all_lessons)
            course_completed = all_done
        
        # Sertifikat mavjudmi
        certificate = Certificate.objects.filter(student=request.user, profession=profession, bundle__isnull=True).first()

    # Yakuniy testlar — alohida UI uçun
    final_test_lessons = []
    has_final_test = False
    if is_enrolled or is_teaching or request.user.is_admin:
        _final_prerequisites_ready = True
        if request.user.is_student:
            _completed_ids = {
                lid for lid, progress in lesson_progress.items() if progress.get('completed')
            }
            _final_prerequisites_ready = final_test_prerequisites_met(
                request.user, profession, _completed_ids,
            )
        final_test_qs = list(Lesson.objects.filter(
            profession=profession,
            lesson_type='test',
        ).filter(final_lesson_q()).select_related('test', 'quiz__settings'))
        has_final_test = bool(final_test_qs)
        for _fl in final_test_qs:
            _payment_status = None
            _legacy_test = getattr(_fl, 'test', None)
            if request.user.is_authenticated and request.user.is_student and _legacy_test:
                _pay = FinalTestPayment.objects.filter(
                    student=request.user, test=_legacy_test
                ).order_by('-created_at').first()
                if _pay:
                    _payment_status = _pay.status
            final_test_lessons.append({
                'lesson': _fl,
                'test': _final_test_info(_fl),
                'payment_status': _payment_status,
                'progress': lesson_progress.get(_fl.pk),
                'token': token_map.get(_fl.pk),
                'locked': request.user.is_student and not _final_prerequisites_ready,
            })

    _roadmap_completed = section_completed if isinstance(locals().get('section_completed'), dict) else {}
    course_roadmap = _build_course_roadmap(
        sections, _roadmap_completed, section_is_locked, section_daily_locked,
    )

    return render(request, 'accounts/profession_detail.html', {
        'profession': profession,
        'is_enrolled': is_enrolled,
        'is_teaching': is_teaching,
        'enrollment_request': enrollment_request,
        'unenroll_request': unenroll_request,
        'sections': sections,
        'lessons_without_section': lessons_without_section,
        'lesson_progress': lesson_progress,
        'course_roadmap': course_roadmap,
        'section_is_locked': section_is_locked if (is_enrolled or is_teaching or request.user.is_admin) else {},
        'section_daily_locked': section_daily_locked if (is_enrolled or is_teaching or request.user.is_admin) else {},
        'daily_section_today': daily_section_today if (is_enrolled and request.user.is_student) else None,
        'daily_limit_section_name': daily_limit_section_name if (is_enrolled and request.user.is_student) else None,
        'classmates': classmates,
        'course_teachers': course_teachers,
        'course_completed': course_completed,
        'has_empty_section': has_empty_section if is_enrolled and request.user.is_student else False,
        'certificate': certificate,
        'lesson_is_locked': lesson_is_locked,
        'token_map': token_map,
        'final_test_lessons': final_test_lessons,
        'has_final_test': has_final_test,
    })


def _course_statistics_data(profession, group_id=None):
    """Kurs statistikasi körsatkiçlari (DRY — sahifa view va JSON API uçun).

    ``group_id`` berilsa — reyting (leaderboard) faqat öşa StudyGroup
    a'zolari böyiça hisoblanadi; umumiy kurs körsatkiçlari (``overview``)
    esa har doim butun kurs böyiça qaytariladi, şunda sahifada guruh va
    umumiy statistika birga körinadi.

    ``total_score`` (umumiy ball) — 0..100 oraliğidagi kompozit foiz: test
    örtaça bali va vazifa örtaça bahosining (10 balldan 100 ga keltirilgan)
    örtaçasi. Bu eski "barça urinişlar % yiğindisi" hisobini almaştiradi
    (u qayta urinişlarni qöşib, 689 kabi ma'nosiz sonlar berardi)."""
    from collections import defaultdict
    from django.utils import timezone
    from .models import StudyGroup

    completed_statuses = [Attempt.Status.SUBMITTED, Attempt.Status.GRADED]

    # ── Umumiy: butun kurs böyiça körsatkiçlar ──
    all_enrolled = CustomUser.objects.filter(
        enrollments__profession=profession, role='student'
    ).distinct()
    total_students = all_enrolled.count()

    lessons = Lesson.objects.filter(profession=profession)
    total_lessons = lessons.count()
    total_tests = lessons.filter(lesson_type='test').count()
    total_homeworks = lessons.filter(lesson_type='homework').count()
    total_videos = lessons.filter(lesson_type='video').count()

    course_tests = Attempt.objects.filter(
        quiz__lesson__profession=profession, status__in=completed_statuses,
    )
    avg_test_score = course_tests.aggregate(a=Avg('score'))['a'] or 0
    total_test_attempts = course_tests.count()
    passed_tests = course_tests.filter(passed=True).count()
    pass_rate = round(passed_tests / total_test_attempts * 100, 1) if total_test_attempts else 0

    course_hw = HomeworkSubmission.objects.filter(homework__lesson__profession=profession)
    total_hw_submitted = course_hw.count()
    avg_hw_grade = course_hw.filter(status='graded').aggregate(a=Avg('grade'))['a'] or 0

    overview = {
        'total_students': total_students,
        'total_lessons': total_lessons,
        'total_tests': total_tests,
        'total_homeworks': total_homeworks,
        'total_videos': total_videos,
        'avg_test_score': round(avg_test_score, 1),
        'pass_rate': pass_rate,
        'total_test_attempts': total_test_attempts,
        'total_hw_submitted': total_hw_submitted,
        'avg_hw_grade': round(avg_hw_grade, 1),
    }

    # ── Guruhlar röyxati (tanlagiç uçun) ──
    groups_qs = StudyGroup.objects.filter(profession=profession).order_by('name')
    groups = [
        {'id': g.id, 'name': g.name, 'count': g.students.filter(role='student').count()}
        for g in groups_qs
    ]

    # ── Reyting doirasi (scope): tanlangan guruh yoki butun kurs ──
    selected_group = None
    scope_students = all_enrolled
    if group_id:
        grp = groups_qs.filter(pk=group_id).first()
        if grp:
            selected_group = {'id': grp.id, 'name': grp.name}
            # Guruhga biriktirişning özi şu statistika uçun manba hisoblanadi.
            # Eski/qölda biriktirilgan öquvçida CourseEnrollment bölmasligi
            # mumkin; all_enrolled bilan kesişiş bunday a'zolarni yaşirardi.
            scope_students = grp.students.filter(role='student')

    scope_students = scope_students.order_by('-coins').distinct()
    student_ids = list(scope_students.values_list('id', flat=True))

    # Bulk test statistikasi (scope)
    test_stats = defaultdict(lambda: {'count': 0, 'passed': 0, 'avg_score': 0})
    for row in Attempt.objects.filter(
        student_id__in=student_ids,
        quiz__lesson__profession=profession,
        status__in=completed_statuses,
    ).values('student_id').annotate(
        count=Count('id'),
        passed=Count('id', filter=Q(passed=True)),
        avg_score=Avg('score'),
    ):
        test_stats[row['student_id']] = {
            'count': row['count'],
            'passed': row['passed'],
            'avg_score': row['avg_score'] or 0,
        }

    # Bulk homework statistikasi (scope)
    hw_stats = defaultdict(lambda: {'count': 0, 'graded_avg': 0, 'graded_count': 0, 'accepted_count': 0})
    for row in HomeworkSubmission.objects.filter(
        student_id__in=student_ids, homework__lesson__profession=profession
    ).values('student_id').annotate(
        count=Count('id'),
        graded_avg=Avg('grade', filter=Q(status='graded')),
        graded_count=Count('id', filter=Q(status='graded')),
        accepted_count=Count('id', filter=Q(status__in=['accepted', 'graded'])),
    ):
        hw_stats[row['student_id']] = {
            'count': row['count'],
            'graded_avg': row['graded_avg'] or 0,
            'graded_count': row['graded_count'],
            'accepted_count': row['accepted_count'],
        }

    # Bulk video progress (scope)
    video_stats = dict(
        VideoProgress.objects.filter(
            user_id__in=student_ids, watched=True,
            video__lesson__profession=profession
        ).values('user_id').annotate(c=Count('id')).values_list('user_id', 'c')
    )

    total_possible = total_tests + total_homeworks + total_videos

    students = []
    distribution = {'high': 0, 'medium': 0, 'low': 0}
    grp_score_sum = grp_score_n = 0
    grp_test_sum = grp_test_n = 0
    grp_pass = grp_attempts = 0

    seen_student_ids = set()
    for student in scope_students:
        sid = student.id
        if sid in seen_student_ids:
            continue
        seen_student_ids.add(sid)
        ts = test_stats[sid]
        hs = hw_stats[sid]
        vw = video_stats.get(sid, 0)

        completed = ts['passed'] + hs['accepted_count'] + vw
        progress = min(round(completed / total_possible * 100) if total_possible else 0, 100)

        # ── Töğri umumiy ball: 0..100 kompozit ──
        components = []
        if ts['count'] > 0:
            components.append(ts['avg_score'])          # test örtaça (0..100)
        if hs['graded_count'] > 0:
            components.append(hs['graded_avg'] * 10)     # vazifa örtaça (0..10 → 0..100)
        overall_score = round(sum(components) / len(components)) if components else 0

        if progress >= 70:
            distribution['high'] += 1
        elif progress >= 40:
            distribution['medium'] += 1
        else:
            distribution['low'] += 1

        photo_url = ''
        try:
            if student.photo:
                photo_url = student.photo.url
        except Exception:
            photo_url = ''

        full_name = (f"{student.first_name} {student.last_name}").strip() or student.username

        students.append({
            'id': sid,
            'name': full_name,
            'initial': (student.first_name[:1] or student.username[:1] or '?').upper(),
            'photo_url': photo_url,
            'test_count': ts['count'],
            'test_avg': round(ts['avg_score'], 1),
            'passed_tests': ts['passed'],
            'hw_count': hs['count'],
            'hw_avg': round(hs['graded_avg'], 1),
            'videos_watched': vw,
            'progress': progress,
            'coins': student.coins,
            'total_score': overall_score,
        })

        grp_score_sum += overall_score
        grp_score_n += 1
        if ts['count']:
            grp_test_sum += ts['avg_score']
            grp_test_n += 1
        grp_pass += ts['passed']
        grp_attempts += ts['count']

    students.sort(key=lambda x: (x['total_score'], x['coins']), reverse=True)
    for i, s in enumerate(students):
        s['rank'] = i + 1

    group_overview = {
        'students': len(students),
        'avg_score': round(grp_score_sum / grp_score_n) if grp_score_n else 0,
        'avg_test': round(grp_test_sum / grp_test_n, 1) if grp_test_n else 0,
        'pass_rate': round(grp_pass / grp_attempts * 100, 1) if grp_attempts else 0,
    }

    return {
        'profession': {'id': profession.pk, 'name': profession.name},
        'overview': overview,
        'group_overview': group_overview,
        'groups': groups,
        'selected_group': selected_group,
        'distribution': distribution,
        'students': students,
        'generated_at': timezone.localtime().strftime('%H:%M:%S'),
    }


def _can_view_course_stats(user, profession):
    """Statistikani köriş ruxsatini tekşiradi."""
    is_enrolled = CourseEnrollment.objects.filter(
        user=user, profession=profession
    ).exists() or user.profession == profession
    return is_enrolled or user.is_admin or user.is_teacher


def _resolve_stats_group(request, profession):
    """Tanlangan guruh id'sini aniqlaydi.

    Öquvçi uçun URL'dagi ``?group=`` ga işonilmaydi: doim uning şu
    kursdagi guruhi olinadi. Şunda bir guruh a'zolari aynan bir xil statistik
    doirani köradi. Öqituvchi/admin esa guruh filtrini erkin tanlay oladi.
    """
    user = request.user
    if getattr(user, 'role', None) == 'student':
        my_group = user.study_groups.filter(profession=profession).first()
        if my_group:
            return str(my_group.id)
        return ''
    return (request.GET.get('group') or '').strip()


@login_required
def course_statistics(request, pk):
    profession = get_object_or_404(Profession, pk=pk)
    if not _can_view_course_stats(request.user, profession):
        messages.error(request, "Bu kursga yozilmagansiz!")
        return redirect('profession_detail', pk=pk)

    group_id = _resolve_stats_group(request, profession)
    data = _course_statistics_data(profession, group_id=group_id or None)
    my_group = None
    if getattr(request.user, 'role', None) == 'student':
        mg = request.user.study_groups.filter(profession=profession).first()
        my_group = mg.id if mg else None
    return render(request, 'accounts/course_statistics.html', {
        'profession': profession,
        'overview': data['overview'],
        'stats_data': data,
        'my_group_id': my_group,
    })


@login_required
def course_statistics_api(request, pk):
    """Kurs statistikasi JSON API — jonli yangilaniş, grafiklar va eksport uçun."""
    profession = get_object_or_404(Profession, pk=pk)
    if not _can_view_course_stats(request.user, profession):
        return JsonResponse({'error': "Bu kursga ruxsatingiz yöq"}, status=403)
    group_id = _resolve_stats_group(request, profession)
    return JsonResponse(_course_statistics_data(profession, group_id=group_id or None))


@login_required
def enroll_course(request, pk):
    if not request.user.is_learner:
        messages.error(request, "Faqat öquvçilar kursga yozilişi mumkin!")
        return redirect('professions')

    # Kursni tugatgan öquvçilar yangi kurslarga yozila olmaydi
    if request.user.is_finished:
        messages.warning(request, "Siz kursni tugatgan öquvçi sifatida belgilangansiz. Yangi kurslarga yozila olmaysiz. Iltimos, admin bilan boğlaning.")
        return redirect('profession_detail', pk=pk)
    
    profession = get_object_or_404(Profession, pk=pk)
    
    # Majburiy kurs — avtomatik yozilgan bölişi kerak
    if profession.is_mandatory:
        CourseEnrollment.objects.get_or_create(user=request.user, profession=profession)
        messages.info(request, f"'{profession.name}' majburiy kurs — siz avtomatik yozilgansiz!")
        return redirect('profession_detail', pk=pk)
    
    # Allaqaçon yozilganmi?
    if CourseEnrollment.objects.filter(user=request.user, profession=profession).exists():
        messages.info(request, f"Siz allaqaçon '{profession.name}' kursiga yozilgansiz!")
        return redirect('profession_detail', pk=pk)
    
    from .course_membership import has_teachers, notify_new_enroll_request, notify_student_joined

    # Bepul kurs - töğridan-töğri yoziş (GET yoki POST).
    # Kursga öqituvçi biriktirilgan bölsa — yozilişni öqituvçi tasdiqlaydi.
    if not profession.is_paid and not has_teachers(profession):
        # Eski sörovlarni tozalaş (qayta yozilişga tösqinlik qilmasligi uçun)
        CourseUnenrollRequest.objects.filter(user=request.user, profession=profession).delete()
        CourseEnrollmentRequest.objects.filter(user=request.user, profession=profession).delete()
        _, created = CourseEnrollment.objects.get_or_create(user=request.user, profession=profession)
        if created:
            # Qayta yoziliş bölsa — eski progressni tozalaş
            _reset_course_progress(request.user, profession)
        notify_admins(
            f"📚 <b>Kursga yozildi!</b>\n\n"
            f"👤 {request.user.first_name} {request.user.last_name}\n"
            f"📖 {profession.name}\n"
            f"💰 Bepul kurs"
        )
        notify_student_joined(request.user, profession, how="Bepul kurs — özi yozildi")
        messages.success(request, f"'{profession.name}' kursiga muvaffaqiyatli yozildingiz! Öqişni boşlaşingiz mumkin.")
        return redirect('profession_detail', pk=pk)

    # Bir martalik tölov - sörov örniga Tezcheck tölov sahifasi
    if profession.is_sold_once:
        from .course_payments import course_context, enroll_paid_student, has_paid_course
        if has_paid_course(request.user, profession):
            enroll_paid_student(request.user, profession)
            messages.success(request, f"'{profession.name}' kursi sotib olingan — öqişni davom ettirişingiz mumkin.")
            return redirect('profession_detail', pk=pk)
        return render(request, 'accounts/course_checkout.html', {
            'profession': profession, **course_context(request.user, profession),
        })

    # Pullik kurs - sörov yuboriş
    existing_request = CourseEnrollmentRequest.objects.filter(user=request.user, profession=profession).first()
    
    if existing_request:
        if existing_request.status == 'pending':
            messages.info(request, f"Sizning '{profession.name}' kursiga sörovingiz öqituvchi/admin tomonidan körib çiqilmoqda.")
            return redirect('profession_detail', pk=pk)
        elif existing_request.status in ('approved', 'rejected'):
            # Eski sörovni öçirib, qayta sörov yuborişga ruxsat beriş
            existing_request.delete()
    
    if request.method == 'POST':
        reason = request.POST.get('reason', '').strip()
        if not reason:
            messages.error(request, "Iltimos, kursga yoziliş sababini yozing!")
            return render(request, 'accounts/enroll_confirm.html', {'profession': profession})
        messages.success(request, f"'{profession.name}' kursiga yoziliş sörovi yuborildi! Öqituvçi yoki admin tasdiqlaşini kuting.")
        enroll_request = CourseEnrollmentRequest.objects.create(user=request.user, profession=profession, reason=reason)
        notify_admins(
            f"📋 <b>Kursga yoziliş sörovi!</b>\n\n"
            f"👤 {request.user.first_name} {request.user.last_name}\n"
            f"📖 {profession.name}\n"
            f"{'💰 Pullik kurs' if profession.is_paid else '🆓 Bepul kurs'} — tasdiq kutilmoqda"
        )
        notify_new_enroll_request(enroll_request)
        return redirect('profession_detail', pk=pk)
    
    return render(request, 'accounts/enroll_confirm.html', {'profession': profession})


@login_required
def unenroll_course(request, pk):
    if not request.user.is_learner:
        messages.error(request, "Faqat öquvçilar kursdan çiqişi mumkin!")
        return redirect('professions')
    
    profession = get_object_or_404(Profession, pk=pk)
    enrollment = CourseEnrollment.objects.filter(user=request.user, profession=profession).first()
    is_enrolled = enrollment or request.user.profession == profession
    
    if not is_enrolled:
        messages.warning(request, "Siz bu kursga yozilmagansiz!")
        return redirect('professions')
    
    if profession.is_mandatory:
        messages.error(request, f"'{profession.name}' majburiy kurs — undan çiqiş mumkin emas!")
        return redirect('profession_detail', pk=pk)
    
    # Allaqaçon kutilayotgan sörov bormi?
    existing = CourseUnenrollRequest.objects.filter(user=request.user, profession=profession, status='pending').first()
    if existing:
        messages.info(request, "Sizning kursdan çiqiş sörovingiz allaqaçon yuborilgan. Öqituvçi yoki admin tasdiqlaşini kuting.")
        return redirect('profession_detail', pk=pk)
    
    if request.method == 'POST':
        reason = request.POST.get('reason', '').strip()
        if not reason:
            messages.error(request, "Iltimos, kursdan çiqiş sababini yozing!")
            return redirect('profession_detail', pk=pk)
        
        # Eski yakunlangan sörovlarni öçiriş (rejected yoki approved)
        CourseUnenrollRequest.objects.filter(
            user=request.user, profession=profession
        ).exclude(status='pending').delete()
        
        unenroll_request = CourseUnenrollRequest.objects.create(
            user=request.user,
            profession=profession,
            reason=reason,
        )
        from .course_membership import notify_new_unenroll_request
        notify_new_unenroll_request(unenroll_request)
        notify_admins(
            f"🚪 <b>Kursdan çiqiş sörovi!</b>\n\n"
            f"👤 {request.user.first_name} {request.user.last_name}\n"
            f"📖 {profession.name}\n"
            f"📝 Sabab: {reason[:200]}"
        )
        messages.success(request, f"Kursdan çiqiş sörovi yuborildi. Öqituvçi yoki admin körib çiqişini kuting.")
        return redirect('profession_detail', pk=pk)
    
    return redirect('profession_detail', pk=pk)


@login_required
def request_certificate(request, pk):
    """Öquvçi: Kursni tugatdim - sertifikat sörovi yuboriş"""
    if not request.user.is_learner:
        messages.error(request, "Faqat öquvçilar sertifikat sörovi yuborişi mumkin!")
        return redirect('profession_detail', pk=pk)
    
    profession = get_object_or_404(Profession, pk=pk)
    
    # Bu kurs uçun sertifikat joriy qilinmagan bölsa
    if not profession.has_certificate:
        messages.error(request, "Bu kurs uçun sertifikat joriy qilinmagan.")
        return redirect('profession_detail', pk=pk)

    if not Lesson.objects.filter(profession=profession).filter(final_lesson_q()).exists():
        messages.error(
            request,
            "Bu kursda yakuniy test mavjud emas. Sertifikat oliş uçun yakuniy test bölişi kerak.",
        )
        return redirect('profession_detail', pk=pk)
    
    enrollment = CourseEnrollment.objects.filter(user=request.user, profession=profession).exists()
    if not enrollment and request.user.profession != profession:
        messages.warning(request, "Siz bu kursga yozilmagansiz!")
        return redirect('profession_detail', pk=pk)
    
    # Allaqaçon sertifikat berilganmi
    if Certificate.objects.filter(student=request.user, profession=profession, bundle__isnull=True).exists():
        messages.info(request, "Sizga bu kurs uçun allaqaçon sertifikat berilgan!")
        return redirect('profession_detail', pk=pk)

    # Bölimlar içida dars yöq bölsa — sertifikat oliş mumkin emas
    course_sections = profession.sections.filter(is_active=True)
    if course_sections.exists() and any(not s.lessons.exists() for s in course_sections):
        messages.error(request, "Kurs hali töliq yuklanmagan. Ustoz barça bölimlarga dars qöşişi kerak!")
        return redirect('profession_detail', pk=pk)
    
    # Kurs tugatilganligini tekşiriş
    all_lessons = Lesson.objects.filter(profession=profession).exclude(lesson_type='dictionary')
    if all_lessons.exists():
        watched_videos = set(VideoProgress.objects.filter(
            user=request.user, watched=True, video__lesson__profession=profession
        ).values_list('video__lesson_id', flat=True))
        passed_tests = set(TestResult.objects.filter(
            student=request.user, test__lesson__profession=profession, passed=True
        ).values_list('test__lesson_id', flat=True))
        from quiz.models import Attempt as QuizAttempt
        passed_tests |= set(QuizAttempt.objects.filter(
            student=request.user, quiz__lesson__profession=profession, passed=True,
            status__in=[QuizAttempt.Status.SUBMITTED, QuizAttempt.Status.GRADED],
        ).values_list('quiz__lesson_id', flat=True))
        accepted_homeworks = set(HomeworkSubmission.objects.filter(
            student=request.user, homework__lesson__profession=profession,
            status__in=['accepted', 'graded']
        ).values_list('homework__lesson_id', flat=True))
        graded_speakings = set(SpeakingSubmission.objects.filter(
            student=request.user, speaking_task__lesson__profession=profession,
            status='graded'
        ).values_list('speaking_task__lesson_id', flat=True))
        completed_ids = watched_videos | passed_tests | accepted_homeworks | graded_speakings
        
        for lesson in all_lessons:
            if lesson.pk not in completed_ids:
                messages.error(request, "Kursni tugatiş uçun barça darslarni (video, test, vazifa) bajarişingiz kerak!")
                return redirect('profession_detail', pk=pk)
    
    if request.method == 'POST':
        # 🎓 AVTOMATIK BERIŞ: sörov örniga sertifikat darhol yaratiladi.
        from .certificates import issue_course_certificate
        cert, error = issue_course_certificate(request.user, profession)
        if cert:
            messages.success(
                request,
                f"🎓 Tabriklaymiz! Sertifikatingiz tayyor (seriya: {cert.certificate_id}). "
                f"Yuklab olişingiz mumkin!"
            )
            return redirect('download_certificate', pk=cert.pk)
        messages.error(request, error or "Sertifikat berişda xatolik yuz berdi.")
        return redirect('profession_detail', pk=pk)

    return redirect('profession_detail', pk=pk)


@login_required
def download_certificate(request, pk):
    """Sertifikat PDF faylini yuklab oliş (egasi yoki admin)."""
    cert = get_object_or_404(
        Certificate.objects.select_related('student', 'profession'), pk=pk
    )
    if not (request.user.is_admin or cert.student_id == request.user.pk):
        messages.error(request, "Bu sertifikat sizga tegişli emas!")
        return redirect('home')

    # Fayl yöq yoki eskirgan bölsa — qayta yaratiş
    from .certificates import regenerate_certificate_file
    if not cert.file:
        if not regenerate_certificate_file(cert):
            messages.error(request, "Sertifikat faylini yaratişda xatolik. Keyinroq urinib köring.")
            return redirect('profession_detail', pk=cert.profession_id)

    try:
        response = HttpResponse(cert.file.read(), content_type='application/pdf')
        response['Content-Disposition'] = (
            f'attachment; filename="sertifikat_{cert.certificate_id}.pdf"'
        )
        return response
    except Exception:
        messages.error(request, "Faylni öqişda xatolik yuz berdi.")
        return redirect('profession_detail', pk=cert.profession_id)


@login_required
def admin_certificate_settings(request):
    """Admin: kurslar böyiça sertifikat seriyasi va muddatini sozlaş."""
    if not request.user.is_admin:
        return redirect('home')

    from .models import CertificateSettings

    if request.method == 'POST':
        profession_id = request.POST.get('profession_id', '').strip()
        series_code = request.POST.get('series_code', '').strip().upper()
        validity_years = _to_int(request.POST.get('validity_years'), 2)
        validity_years = min(50, max(1, validity_years))

        profession = get_object_or_404(Profession, pk=profession_id)
        # Seriya kodi boşqa kursda işlatilmaganini tekşiriş
        if series_code and CertificateSettings.objects.filter(
            series_code=series_code
        ).exclude(profession=profession).exists():
            messages.error(request, f"\"{series_code}\" seriyasi boşqa kursda işlatilgan!")
            return redirect('admin_certificates')

        from .models import CertificateDesign
        design_id = request.POST.get('design_id', '').strip()
        design = CertificateDesign.objects.filter(pk=design_id).first() if design_id.isdigit() else None

        cfg, _ = CertificateSettings.objects.get_or_create(profession=profession)
        design_changed = cfg.design_id != (design.pk if design else None)
        cfg.series_code = series_code
        cfg.validity_years = validity_years
        cfg.design = design
        cfg.save()
        messages.success(
            request,
            f"\"{profession.name}\" kursi sertifikat sozlamalari saqlandi "
            f"({series_code or 'seriyasiz'}, {validity_years} yil, "
            f"dizayn: {design.name if design else 'standart'})."
        )
        # Dizayn almaşgan bölsa — mavjud sertifikatlar PDF'i ham yangilanadi
        if design_changed and request.POST.get('regenerate') == '1':
            from .certificate_designs import regenerate_certificates
            ok, fail = regenerate_certificates(profession.certificates.all())
            if ok or fail:
                messages.info(request, f"{ok} ta sertifikat yangi dizaynda qayta yaratildi"
                                       + (f", {fail} tasida xatolik." if fail else "."))
    return redirect('admin_certificates')


@login_required
def admin_certificate_edit(request, pk):
    """Admin: sertifikat ma'lumotlarini tahrirlaş (holat, muddat, tavsif).

    Özgarişdan söng PDF avtomatik qayta yaratiladi — sertifikatdagi
    ma'lumotlar ham yangilanadi.
    """
    if not request.user.is_admin:
        return redirect('home')

    cert = get_object_or_404(Certificate, pk=pk)

    if request.method == 'POST':
        new_status = request.POST.get('status', cert.status)
        valid_until = request.POST.get('valid_until', '').strip()
        description = request.POST.get('description')

        if new_status in dict(Certificate.STATUS_CHOICES):
            cert.status = new_status
        if valid_until:
            try:
                from datetime import datetime as _dt
                cert.valid_until = _dt.strptime(valid_until, '%Y-%m-%d').date()
            except ValueError:
                messages.error(request, "Sana formati notöğri!")
                return redirect('admin_certificates')
        if description is not None:
            cert.description = description.strip()
        cert.save()

        # PDF ni qayta yaratiş — ma'lumotlar sertifikatda ham yangilanadi
        from .certificates import regenerate_certificate_file
        regenerate_certificate_file(cert)

        # Öquvçiga xabar (platformadagi öquvçi bölsa)
        if cert.student:
            Message.objects.create(
                recipient=cert.student,
                sender=request.user,
                title="📋 Sertifikat ma'lumotlari yangilandi",
                content=(
                    f"Sizning \"{cert.course_name}\" kursi böyiça sertifikatingiz "
                    f"ma'lumotlari yangilandi.\n\n"
                    f"🏅 Seriya: {cert.certificate_id}\n"
                    f"📌 Holat: {cert.get_status_display()}\n"
                    f"⏳ Amal qiliş muddati: "
                    f"{cert.valid_until.strftime('%d.%m.%Y') if cert.valid_until else '—'}"
                ),
                message_type='system',
            )
        messages.success(request, f"Sertifikat {cert.certificate_id} yangilandi va PDF qayta yaratildi!")

    return redirect('admin_certificates')


@login_required
def admin_certificate_manual(request):
    """Admin: qölda sertifikat beriş — ism-familiya + kurs => tayyor PDF.

    Ism erkin kiritiladi (platformada bölmagan odam uçun ham) yoki
    mavjud öquvçi tanlanadi.
    """
    if not request.user.is_admin:
        return redirect('home')

    from .models import CertificateSettings
    _settings_map = {
        s.profession_id: s for s in CertificateSettings.objects.all()
    }
    professions = []
    for p in Profession.objects.all():
        s = _settings_map.get(p.pk)
        professions.append({
            'obj': p,
            'series_code': s.series_code if s else '',
            'validity_years': s.validity_years if s else 2,
        })
    students = CustomUser.objects.filter(role='student').order_by('first_name', 'last_name')

    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        student_id = request.POST.get('student_id', '').strip()
        profession_id = request.POST.get('profession_id', '').strip()

        if not profession_id:
            messages.error(request, "Kursni tanlang!")
            return redirect('admin_certificate_manual')

        profession = get_object_or_404(Profession, pk=profession_id)
        student = None
        if student_id:
            student = get_object_or_404(CustomUser, pk=student_id, role='student')

        from .certificates import issue_manual_certificate
        cert, error = issue_manual_certificate(
            profession=profession,
            issued_by=request.user,
            first_name=first_name,
            last_name=last_name,
            student=student,
        )
        if cert and not error:
            messages.success(
                request,
                f"🎓 Sertifikat tayyor! {cert.recipient_display} — "
                f"{profession.name} (seriya: {cert.certificate_id})"
            )
            return redirect('download_certificate', pk=cert.pk)
        messages.error(request, error or "Sertifikat berişda xatolik yuz berdi.")
        return redirect('admin_certificate_manual')

    return render(request, 'accounts/admin/certificate_manual.html', {
        'professions': professions,
        'students': students,
    })


@login_required
def admin_certificate_regenerate(request, pk):
    """Admin: sertifikat PDF faylini qölda qayta yaratiş."""
    if not request.user.is_admin:
        return redirect('home')
    cert = get_object_or_404(Certificate, pk=pk)
    from .certificates import regenerate_certificate_file
    if regenerate_certificate_file(cert):
        messages.success(request, f"Sertifikat {cert.certificate_id} PDF fayli qayta yaratildi!")
    else:
        messages.error(request, "PDF yaratişda xatolik yuz berdi.")
    return redirect('admin_certificates')


# Lesson views
def _is_section_locked_for_student(user, section, profession):
    """Öquvçi uçun bölim qulflanganmi - oldingi bölimdagi barça darslar bajarilganmi"""
    if not user.is_learner or user.is_teacher or user.is_admin:
        return False
    sections = profession.sections.filter(is_active=True).prefetch_related('lessons').order_by('order')
    sections_list = list(sections)
    try:
        section_idx = next(i for i, s in enumerate(sections_list) if s.id == section.id)
    except StopIteration:
        return False
    if section_idx == 0:
        return False
    # Oldingi bölimlardagi barça darslarni tekşiriş
    watched_videos = set(VideoProgress.objects.filter(
        user=user, watched=True,
        video__lesson__profession=profession
    ).values_list('video__lesson_id', flat=True))
    passed_tests = set(TestResult.objects.filter(
        student=user, test__lesson__profession=profession, passed=True
    ).values_list('test__lesson_id', flat=True))
    from quiz.models import Attempt as QuizAttempt
    passed_tests |= set(QuizAttempt.objects.filter(
        student=user, quiz__lesson__profession=profession, passed=True,
        status__in=[QuizAttempt.Status.SUBMITTED, QuizAttempt.Status.GRADED],
    ).values_list('quiz__lesson_id', flat=True))
    accepted_homeworks = set(HomeworkSubmission.objects.filter(
        student=user, homework__lesson__profession=profession,
        status__in=['accepted', 'graded']
    ).values_list('homework__lesson_id', flat=True))
    completed_lesson_ids = watched_videos | passed_tests | accepted_homeworks
    for prev_section in sections_list[:section_idx]:
        for prev_lesson in prev_section.lessons.all():
            if prev_lesson.lesson_type == 'dictionary':
                continue
            if prev_lesson.pk not in completed_lesson_ids:
                return True
    return False


@login_required
def lesson_view(request, pk):
    lesson = get_object_or_404(Lesson, pk=pk)
    redir = _redirect_student_to_token(request, lesson)
    if redir:
        return redir
    profession = lesson.profession

    context = {
        'lesson': lesson,
        'profession': profession,
        'video_timer_debug': settings.DEBUG,
    }
    
    # Keyingi darsni topiş
    next_lesson = None
    if lesson.section:
        next_lesson = Lesson.objects.filter(
            profession=profession,
            section=lesson.section,
            order__gt=lesson.order
        ).first()
        if not next_lesson:
            next_section = Section.objects.filter(
                profession=profession,
                is_active=True,
                order__gt=lesson.section.order
            ).first()
            if next_section:
                next_lesson = Lesson.objects.filter(
                    profession=profession,
                    section=next_section
                ).first()
    else:
        next_lesson = Lesson.objects.filter(
            profession=profession,
            section__isnull=True,
            order__gt=lesson.order
        ).first()
    context['next_lesson'] = next_lesson
    
    if lesson.lesson_type == 'video':
        video = lesson.video
        progress, _ = VideoProgress.objects.get_or_create(user=request.user, video=video)
        # Sahifa render QILINIŞIDANOQ talab vaqti server tomonda yakuniy
        # holatda bölişi kerak: aks holda birinçi heartbeat'da
        # required_seconds qayta hisoblanib, ekrandagi countdown sakrab ketadi.
        _ensure_gating_duration(video)
        _resolve_required_seconds(progress, video)
        start_watch_session(progress)
        # AI maqola yaratiş (birinçi marta) — faqat threadda, requestni bloklamaymiz
        if not lesson.ai_article:
            import threading
            _lesson_id = lesson.pk
            _title = lesson.title
            _prof_name = profession.name
            def _bg_generate():
                try:
                    from accounts.models import Lesson as _Lesson
                    art = _generate_ai_article(_title, _prof_name)
                    if art:
                        _Lesson.objects.filter(pk=_lesson_id, ai_article__isnull=True).update(ai_article=art)
                except Exception:
                    pass
            threading.Thread(target=_bg_generate, daemon=True).start()
        context['video'] = video
        context['progress'] = progress
        context['resume_at'] = progress.last_position if not progress.watched else 0
        context['video_coin_amount'] = _video_watch_coin_amount(video, request.user)
        return render(request, 'accounts/lessons/video_lesson.html', context)
    
    elif lesson.lesson_type == 'homework':
        homework = lesson.homework
        submission = HomeworkSubmission.objects.filter(homework=homework, student=request.user).first()
        context['homework'] = homework
        context['submission'] = submission
        # Sidebar'dagi "Keyingi dars" kartasi şu bayroqqa qarab
        # körsatiladi/berkitiladi -- vazifa hali tasdiqlanmagan bölsa
        # (pending/reviewing/revision) keyingi darsga ötiş tugmasi
        # körinmaydi, chunki server tomonida ham xuddi şu qoida
        # (student_lesson_view'dagi homework_locked_ids tekşiruvi) bilan
        # bloklanadi -- ikkalasi bir xil holatni aks ettirişi kerak.
        # Vazifa TOPŞIRILGAÇ keyingi darsga (test) ötiş kartasi oçiladi — tasdiqni kutmaydi
        context['homework_approved'] = bool(
            not next_lesson
            or next_lesson.section_id == lesson.section_id
            or submission
        )
        context['form'] = HomeworkSubmissionForm()
        return render(request, 'accounts/lessons/homework_lesson.html', context)
    
    elif lesson.lesson_type == 'test':
        quiz = getattr(lesson, 'quiz', None)
        test = getattr(lesson, 'test', None)
        is_staff = request.user.is_admin or request.user.is_assistant_admin or request.user.is_teacher
        # Yangi quiz tizimi (agar mavjud bölsa ustun turadi). Öquvçi faqat
        # e'lon qilingan quizga yuboriladi; qoralama quizda eski test oçiladi.
        if quiz and is_staff:
            return redirect('quizui:quiz_edit', pk=quiz.pk)
        if quiz and quiz.status == Quiz.Status.PUBLISHED:
            return redirect('quizui:quiz_take', pk=quiz.pk)
        # Eski test tizimi
        if test:
            result = TestResult.objects.filter(test=test, student=request.user).order_by('-completed_at').first()
            context['test'] = test
            context['result'] = result
            if test.test_type == 'final':
                pending_payment = FinalTestPayment.objects.filter(
                    student=request.user, test=test, status='pending'
                ).exists() if result else False
                approved_payment = FinalTestPayment.objects.filter(
                    student=request.user, test=test, status='approved'
                ).exists() if result else False
                context['pending_payment'] = pending_payment
                context['approved_payment'] = approved_payment
                return render(request, 'accounts/lessons/final_test_lesson.html', context)
            return render(request, 'accounts/lessons/test_lesson.html', context)
        if is_staff:
            messages.warning(request, "Bu dars uçun quiz yaratilmagan. Quiz yarating.")
            return redirect('quizui:quiz_create')
        messages.error(request, "Bu test hali tayyor emas.")
        return redirect('profession_detail', pk=lesson.profession.pk)

    elif lesson.lesson_type == 'dictionary':
        dictionary = lesson.dictionary
        words = dictionary.words.all()
        search = request.GET.get('q', '').strip()
        if search:
            words = words.filter(
                models.Q(term__icontains=search) | models.Q(translation__icontains=search)
            )
        context['dictionary'] = dictionary
        context['words'] = words
        context['search'] = search
        return render(request, 'accounts/lessons/dictionary_lesson.html', context)
    
    elif lesson.lesson_type == 'speaking':
        speaking_task = lesson.speaking_task
        submission = SpeakingSubmission.objects.filter(speaking_task=speaking_task, student=request.user).first()
        context['speaking_task'] = speaking_task
        context['submission'] = submission
        return render(request, 'accounts/lessons/speaking_lesson.html', context)
    
    return redirect('profession_detail', pk=profession.pk)


@login_required
def submit_speaking(request, pk):
    """Speaking vazifani topşiriş"""
    speaking_task = get_object_or_404(SpeakingTask, pk=pk)
    redir = _redirect_student_to_token(request, speaking_task.lesson)
    if redir:
        return redir
    
    if request.method != 'POST':
        return redirect('lesson_view', pk=speaking_task.lesson.pk)
    
    existing = SpeakingSubmission.objects.filter(speaking_task=speaking_task, student=request.user).first()
    if existing:
        messages.info(request, "Siz bu vazifani allaqaçon topşirgansiz!")
        return redirect('lesson_view', pk=speaking_task.lesson.pk)
    
    audio_file = request.FILES.get('audio_file')
    if not audio_file:
        messages.error(request, "Audio faylni yuklang!")
        return redirect('lesson_view', pk=speaking_task.lesson.pk)
    
    submission = SpeakingSubmission.objects.create(
        speaking_task=speaking_task,
        student=request.user,
        audio_file=audio_file,
    )
    
    # AI avtomatik tekşiruv
    if speaking_task.ai_check and speaking_task.lesson.profession.ai_check_enabled:
        # Audio transkripsiya + baholaş 20–60 soniya oladi — fonda bajariladi.
        from teacher.views import apply_ai_speaking_grade
        ai_jobs.submit(apply_ai_speaking_grade, submission.pk)
        messages.success(request, "Speaking vazifa topşirildi! 🤖 AI uni bir neça daqiqada tekşiradi — natijani şu sahifada körasiz.")
    else:
        messages.success(request, "Speaking vazifa muvaffaqiyatli topşirildi!")
    
    return redirect('lesson_view', pk=speaking_task.lesson.pk)


def _video_watch_coin_amount(video, user):
    """Har bir video daqiqasi uçun 1 coin, premium foydalanuvçiga 2x.

    `duration` (daqiqa) böş bölişi mumkin — server aniqlagan davomiylikdan
    hisoblaymiz, aks holda uzun video uçun 1 coin berilib qolardi.
    """
    seconds = video.gating_duration_seconds
    minutes = max(1, round(seconds / 60)) if seconds else max(1, int(video.duration or 0))
    return minutes * (2 if user.is_premium else 1)


# ── Server tomonlama köriş nazorati (v2: segment hisoboti) ─────────────────
# Arxitektura:
#   • Brauzer pleerni har soniyada öqib, haqiqatan ijro etilgan oraliqlarni
#     ([start, end] segmentlar) ÖZI yozadi. Ekrandagi taymer faqat şu lokal
#     segmentlardan hisoblanadi — tarmoq sekin yoki uzilgan bölsa ham töxtamaydi.
#   • Har ~10 soniyada (va pauza/yakunda) hali tasdiqlanmagan segmentlar serverga
#     yuboriladi. Javob yöqolsa, mijoz ularni qayta yuboradi: segmentlar union
#     qilinadi, şuning uçun takroriy yuboriş ikki marta sanalmaydi.
#   • Server mijozga işonmaydi: har sinxronda qöşiladigan YANGI unique
#     soniyalar oldingi sinxrondan beri ötgan REAL vaqt × SYNC_MAX_RATE dan
#     oşmaydi. Şuning uçun 1 soatlik videoni yopiş uçun baribir ~1 soat
#     kerak; konsoldan soxta segment yuboriş yoki sörov spami yordam bermaydi.
#   • Nonce zanjiri, pozitsiya sakraşi böyiça "jazo" va muddatli tekşiruvlar
#     yöq — aynan ular halol öquvçida taymerni qotirib qöyardi.

from .video_duration import schedule_verified_duration

WATCH_REQUIRED_RATIO = 0.9   # video davomiyligining şunça qismi körilişi şart
# Kredit real vaqtdan tez ösmaydi. 1.3 — tarmoq keçikişi va pleer jitteri
# uçun zaxira. 2x tezlikda körgan öquvçining ortiqça segmentlari
# yöqolmaydi: mijoz ularni navbatda saqlab, keyingi sinxronlarda yuboradi.
SYNC_MAX_RATE = 1.3
# Byudjet şu soniyadan ortiq yiğilmaydi (masalan, keça oçilgan sahifa).
SYNC_MAX_GAP = 300
SYNC_MAX_SEGMENTS = 300

# ── Tiriklik tekşiruvi (presence check) ──
# Server tasodifiy vaqtda "Şu yerdamisiz?" tasdiğini söraydi. Muddat YÖQ:
# javob kelgunça faqat kredit töxtaydi, javob esa istalgan paytda qabul
# qilinadi. Şu sababli keçikib bosilgan tugma taymerni abadiy qotirmaydi.
PRESENCE_FIRST_MIN    = 60
PRESENCE_FIRST_MAX    = 120
PRESENCE_INTERVAL_MIN = 180
PRESENCE_INTERVAL_MAX = 300


def _new_token():
    """Bir martalik presence tokeni."""
    return secrets.token_urlsafe(18)[:24]


def _schedule_next_presence(progress):
    progress.next_check_seconds = progress.verified_seconds + random.uniform(
        PRESENCE_INTERVAL_MIN, PRESENCE_INTERVAL_MAX
    )


def mark_presence_satisfied(user_id, video_id):
    """Tiriklik tekşiruvini bajarilgan deb belgilaydi va navbatdagisini suradi.

    Video testiga javob beriş — tirikligning eng işonçli isboti, şuning
    uçun öquvçi ikki marta bezovta qilinmaydi.
    `video_quiz.views.api_player_submit` dan çaqiriladi.
    """
    with transaction.atomic():
        progress = (
            VideoProgress.objects.select_for_update()
            .filter(user_id=user_id, video_id=video_id)
            .first()
        )
        if progress is None:
            return
        progress.presence_token = ''
        progress.presence_sent_at = None
        _schedule_next_presence(progress)
        progress.save(update_fields=[
            'presence_token', 'presence_sent_at', 'next_check_seconds',
        ])


def _unanswered_mid_quiz_count(user_id, video, required_seconds):
    """Körilişi ŞART bölgan qismdagi javob berilmagan testlar soni.

    `required_seconds`dan keyingi (video oxiridagi) testlar hisobga olinmaydi:
    talab davomiylikning 90% i, şuning uçun oxirgi 10% dagi test darsni
    yopişga tösqinlik qilsa halol öquvçi qulflanib qolardi.
    """
    from video_quiz.models import VideoQuiz, VideoQuizAttempt

    answered = VideoQuizAttempt.objects.filter(
        student_id=user_id, quiz__video=video
    ).values_list('quiz_id', flat=True)
    qs = VideoQuiz.objects.filter(video=video, is_active=True).exclude(pk__in=answered)
    if required_seconds:
        qs = qs.filter(trigger_second__lte=required_seconds)
    return qs.count()


def _clean_seconds(value, limit=86400):
    """Mijozdan kelgan soniya qiymatini xavfsiz float'ga aylantiradi."""
    if isinstance(value, bool):
        return None
    try:
        secs = float(value)
    except (TypeError, ValueError):
        return None
    if secs != secs or secs in (float('inf'), float('-inf')):   # NaN / inf
        return None
    if secs < 0 or secs > limit:
        return None
    return secs


def _merged_watch_segments(segments, new_segment=None, duration=0):
    """Segmentlarni tozalab birlaştiradi va unique soniyalarni qaytaradi."""
    cleaned = []
    for segment in list(segments or []) + ([new_segment] if new_segment else []):
        if not isinstance(segment, (list, tuple)) or len(segment) != 2:
            continue
        start = _clean_seconds(segment[0])
        end = _clean_seconds(segment[1])
        if start is None or end is None:
            continue
        if duration:
            start = min(start, duration)
            end = min(end, duration)
        if end > start:
            cleaned.append([start, end])

    merged = []
    for start, end in sorted(cleaned):
        if merged and start <= merged[-1][1] + 0.25:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    merged = [[round(start, 3), round(end, 3)] for start, end in merged]
    watched = sum(end - start for start, end in merged)
    return merged, watched


def _uncovered_parts(start, end, merged):
    """[start, end] oraliğining `merged` segmentlar bilan qoplanmagan qismlari."""
    parts = []
    cursor = start
    for s, e in merged:
        if e <= cursor:
            continue
        if s >= end:
            break
        if s > cursor:
            parts.append([cursor, min(s, end)])
        cursor = max(cursor, e)
        if cursor >= end:
            break
    if cursor < end:
        parts.append([cursor, end])
    return [p for p in parts if p[1] - p[0] > 0.001]


def _ensure_gating_duration(video):
    """Işonçli davomiylikni ta'minlaydi (taşqi HTTP — fon threadida)."""
    if video.verified_duration_seconds:
        return video.verified_duration_seconds
    schedule_verified_duration(video)
    return video.gating_duration_seconds


def _resolve_required_seconds(progress, video):
    """Talab vaqtini server tasdiqlagan joriy davomiylik bilan sinxronlaydi.

    Davomiylik FAQAT işonçli manbadan olinadi (YouTube/Vimeo yoki admin).
    Mijoz yuborgan davomiylik bu hisobga kirmaydi.
    """
    secs = video.gating_duration_seconds
    if not secs:
        return progress.required_seconds

    required = max(1, int(secs * WATCH_REQUIRED_RATIO))
    if progress.required_seconds == required:
        return required

    VideoProgress.objects.filter(pk=progress.pk).update(required_seconds=required)
    progress.refresh_from_db(fields=['required_seconds'])
    return progress.required_seconds


def _global_credit_cap(progress, now):
    """Barça videolar böyiça umumiy kredit çegarasi (None — çegara yöq).

    Odam bir vaqtda bitta videoni köradi: parallel tablarda bir neçta videoga
    segment yuborib vaqtni köpaytirib bölmaydi.
    """
    last_other = VideoProgress.objects.filter(
        user_id=progress.user_id, last_credit_at__isnull=False
    ).exclude(pk=progress.pk).aggregate(latest=Max('last_credit_at'))['latest']

    last_any = max(
        (ts for ts in (last_other, progress.last_credit_at) if ts is not None),
        default=None,
    )
    if last_any is None:
        return None
    return max(0.0, (now - last_any).total_seconds()) * SYNC_MAX_RATE


def start_watch_session(progress):
    """Dars sahifasi oçilganda kredit byudjeti şu paytdan boşlanadi.

    Aks holda keça qolgan `last_beat_at` sabab birinçi sinxronda bir neça
    daqiqalik soxta segmentni darhol qabul qilib yuboriş mumkin bölardi.
    """
    if progress.watched:
        return
    VideoProgress.objects.filter(pk=progress.pk).update(last_beat_at=timezone.now())


def _parse_client_segments(raw, duration):
    """Mijoz segmentlarini tozalaydi; notöğri qiymatlar şunçaki taşlanadi."""
    if not isinstance(raw, list):
        return []
    limit = duration if duration > 0 else 86400
    result = []
    for item in raw[:SYNC_MAX_SEGMENTS]:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        start = _clean_seconds(item[0])
        end = _clean_seconds(item[1])
        if start is None or end is None:
            continue
        start, end = min(start, limit), min(end, limit)
        if end - start > 0.001:
            result.append([start, end])
    merged, _ = _merged_watch_segments(result)
    return merged


def _watch_state_payload(progress, video, required):
    duration = video.gating_duration_seconds
    return {
        'ok': True,
        'verified_seconds': round(progress.verified_seconds, 1),
        'required_seconds': required,
        'remaining': (
            0 if progress.is_requirement_met
            else max(0, round(required - progress.verified_seconds, 1))
        ) if required else None,
        'can_complete': progress.is_requirement_met,
        'watched_segments': progress.watched_segments,
        'watched_percentage': round(
            min(100, progress.verified_seconds * 100 / duration), 1
        ) if duration else 0,
        'duration': duration,
        'playback_started_at': timezone.localtime(progress.playback_started_at).isoformat() if progress.playback_started_at else None,
        'requirement_met_at': timezone.localtime(progress.requirement_met_at).isoformat() if progress.requirement_met_at else None,
    }


def _process_watch_beat(request, video, progress_pk):
    """Mijoz yuborgan körilgan segmentlarni real vaqt byudjeti bilan qabul qiladi.

    Sörov: {segments: [[s, e], ...], position, presence_token}
    Javob:  server tasdiqlagan segmentlar, talab, can_complete, kerak bölsa
            presence tokeni va `budget_limited` (qolgan qism keyingi sinxronda).
    """
    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except (ValueError, UnicodeDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    position = _clean_seconds(payload.get('position'))
    presence_reply = str(payload.get('presence_token') or '')[:32]

    now = timezone.now()
    _ensure_gating_duration(video)   # taşqi HTTP — tranzaksiyadan TAŞQARIDA
    duration = float(video.gating_duration_seconds or 0)
    incoming = _parse_client_segments(payload.get('segments'), duration)

    with transaction.atomic():
        progress = VideoProgress.objects.select_for_update().get(pk=progress_pk)
        required = _resolve_required_seconds(progress, video)

        segments, unique_watched = _merged_watch_segments(
            progress.watched_segments, duration=duration,
        )
        # Migratsiyadan oldingi progress yöqolmasin.
        if not segments and progress.verified_seconds > 0:
            legacy_end = min(progress.verified_seconds, duration) if duration else progress.verified_seconds
            segments, unique_watched = _merged_watch_segments([], [0, legacy_end], duration=duration)

        blocked_by = None

        # ── 1. Tiriklik tekşiruvi: javob istalgan paytda qabul qilinadi ──
        if progress.presence_token:
            if presence_reply and presence_reply == progress.presence_token:
                progress.presence_token = ''
                progress.presence_sent_at = None
                _schedule_next_presence(progress)
            else:
                blocked_by = 'presence'

        # ── 2. Real vaqt byudjeti ──
        if progress.last_beat_at:
            elapsed = (now - progress.last_beat_at).total_seconds()
            elapsed = max(0.0, min(elapsed, SYNC_MAX_GAP))
        else:
            elapsed = 0.0
        budget = elapsed * SYNC_MAX_RATE
        cap = _global_credit_cap(progress, now)
        if cap is not None:
            budget = min(budget, cap)

        # ── 3. Yangi (hali qoplanmagan) soniyalarni byudjet doirasida qöşamiz ──
        credited = 0.0
        budget_limited = False
        if incoming and blocked_by is None:
            new_parts = []
            for start, end in incoming:
                new_parts.extend(_uncovered_parts(start, end, segments))
            remaining_budget = budget
            accepted = []
            for start, end in new_parts:
                if remaining_budget <= 0.001:
                    budget_limited = True
                    break
                length = end - start
                if length > remaining_budget:
                    accepted.append([start, start + remaining_budget])
                    remaining_budget = 0
                    budget_limited = True
                    break
                accepted.append([start, end])
                remaining_budget -= length
            if accepted:
                before = unique_watched
                segments, unique_watched = _merged_watch_segments(
                    segments + accepted, duration=duration,
                )
                credited = max(0.0, unique_watched - before)

        progress.watched_segments = segments
        progress.verified_seconds = unique_watched
        if segments:
            progress.max_position = max(progress.max_position, segments[-1][1])
        if position is not None:
            progress.last_position = min(position, duration) if duration else position
        if credited > 0:
            progress.last_credit_at = now
            if progress.playback_started_at is None:
                progress.playback_started_at = now
        progress.last_beat_at = now

        if required and progress.requirement_met_at is None and progress.is_requirement_met:
            progress.requirement_met_at = now

        # ── 4. Navbatdagi tiriklik tekşiruvi (faqat faol köriş paytida) ──
        if progress.next_check_seconds <= 0:
            progress.next_check_seconds = progress.verified_seconds + random.uniform(
                PRESENCE_FIRST_MIN, PRESENCE_FIRST_MAX
            )
        if (
            not progress.presence_token
            and credited > 0
            and required and not progress.is_requirement_met
            and progress.verified_seconds >= progress.next_check_seconds
        ):
            progress.presence_token = _new_token()
            progress.presence_sent_at = now

        progress.save(update_fields=[
            'max_position', 'last_position', 'verified_seconds', 'watched_segments',
            'last_beat_at', 'last_credit_at', 'presence_token', 'presence_sent_at',
            'next_check_seconds', 'playback_started_at', 'requirement_met_at',
        ])

    result = _watch_state_payload(progress, video, required)
    result.update({
        'credited': round(credited, 1),
        'budget_limited': budget_limited,
        'blocked': blocked_by,
    })
    if progress.presence_token:
        result['presence'] = {'token': progress.presence_token}
    if getattr(request.user, 'is_learner', False):
        result['quiz_pending'] = _unanswered_mid_quiz_count(
            progress.user_id, video, required,
        )
    return result


def _watch_beat_response(request, video, progress):
    return JsonResponse(_process_watch_beat(request, video, progress.pk))


def _beat_rate_limited(request, video_key):
    """Beat oqimini çeklaydi (429) — normal mijoz 15 soniyada bitta yuboradi.

    Faqat FOYDALANUVÇI+VIDEO böyiça çeklanadi, IP böyiça emas: kompyuter
    xonasidagi önlab öquvçi bitta taşqi IP orqali kiradi va `check_rate_limit`
    IP çegarasi "strike" yozadi — 10 strike'dan keyin butun xona 6 soatga
    bloklanardi. Beat sörovi allaqaçon `@login_required`, ya'ni kimligi aniq.
    """
    key = f"video_watch_beat:{request.user.pk}:{video_key}"
    if is_rate_limited(key, limit=50, window_seconds=60):
        return JsonResponse({
            'ok': False,
            'error': "Juda köp sörov yuborildi. Biroz kutib turing.",
        }, status=429)
    return None


@login_required
def video_watch_beat(request, pk):
    """Heartbeat: admin/öqituvchi uçun (pk böyiça)."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)
    limited = _beat_rate_limited(request, pk)
    if limited is not None:
        return limited
    video = get_object_or_404(VideoLesson, pk=pk)
    progress, _ = VideoProgress.objects.get_or_create(user=request.user, video=video)
    return _watch_beat_response(request, video, progress)


@login_required
def student_video_watch_beat(request, token):
    """Heartbeat: öquvçi uçun (token URL böyiça)."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)
    limited = _beat_rate_limited(request, token)
    if limited is not None:
        return limited
    token_obj = _get_token_or_404(request.user, token)
    lesson = token_obj.lesson
    if lesson.lesson_type != 'video':
        return JsonResponse({'ok': False, 'error': 'Not a video lesson'}, status=400)
    video = lesson.video
    progress, _ = VideoProgress.objects.get_or_create(user=request.user, video=video)
    return _watch_beat_response(request, video, progress)


def _watch_requirement_error(progress, video):
    """Talab bajarilmagan bölsa — JsonResponse, aks holda None."""
    if progress.watched:
        return None   # allaqaçon yopilgan — keyin qöşilgan test bloklamasin

    # Video içidagi testlar javobsiz qolsa dars yopilmaydi: skript video
    # mazmunini bilmaydi, şuning uçun testga javob berolmaydi. Faqat
    # öquvçida tekşiriladi — test modali boşqa rollarda işlamaydi.
    if getattr(progress.user, 'is_learner', False):
        pending = _unanswered_mid_quiz_count(
            progress.user_id, video, progress.required_seconds
        )
        if pending:
            return JsonResponse({
                'success': False,
                'error': f"Video içidagi {pending} ta savol javobsiz qoldi. "
                         f"Videoni köriş davomida savollarga javob bering.",
                'quiz_pending': pending,
            }, status=403)

    if progress.is_requirement_met:
        return None

    # Heç qaçon beat yubormagan bölsa talab hali qulflanmagan — şu yerda
    # belgilaymiz, aks holda konsoldan töğridan-töğri sörov yuborgan
    # öquvçiga "davomiylik aniqlanmadi" degan çalğituvçi xabar borardi.
    required = _resolve_required_seconds(progress, video)
    if not required:
        return JsonResponse({
            'success': False,
            'error': "Video davomiyligi aniqlanmadi — dars hozirça yopilmaydi. "
                     "Administratorga xabar berildi.",
        }, status=400)

    remaining = max(0, int(required - progress.verified_seconds))
    minutes, seconds = divmod(remaining, 60)
    return JsonResponse({
        'success': False,
        'error': f"Videoni oxirigaça körmadingiz. Yana {minutes}:{seconds:02d} köriş kerak.",
        'remaining': remaining,
        'verified_seconds': round(progress.verified_seconds, 1),
        'required_seconds': required,
    }, status=403)


def _complete_video_watch(request, video):
    """Progressni qayta tekşirib, dars va coin mukofotini atomik yakunlaydi."""
    coin_amount = _video_watch_coin_amount(video, request.user)
    awarded = False
    completed_now = False

    with transaction.atomic():
        progress, _ = VideoProgress.objects.select_for_update().get_or_create(
            user=request.user, video=video,
        )
        error = _watch_requirement_error(progress, video)
        if error is not None:
            return error

        if not progress.watched:
            progress.watched = True
            progress.watched_at = timezone.now()
            completed_now = True

        # Coin faqat birinçi completion sörovida beriladi. Eski yozuvlarda
        # watched=True bölib coin_awarded=False bölişi mumkin; qayta oçiş
        # ularni orqaga qarab mukofotlamasligi kerak.
        if completed_now and not progress.coin_awarded:
            request.user.add_coins(
                coin_amount, f"Video darslik körildi: {video.lesson.title}",
            )
            progress.coin_awarded = True
            awarded = True
        elif progress.watched and not progress.coin_awarded:
            progress.coin_awarded = True

        progress.save(update_fields=[
            'watched', 'watched_at', 'coin_awarded',
        ])

    if completed_now:
        from .notifications import on_video_watched
        on_video_watched(request.user, progress)

    request.user.refresh_from_db(fields=['coins'])
    return JsonResponse({
        'success': True,
        'coins': request.user.coins,
        'coin_amount': coin_amount if awarded else 0,
        'is_premium': request.user.is_premium,
    })


@login_required
def mark_video_watched(request, pk):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST only'})

    if request.user.is_student and not request.user.is_admin:
        return JsonResponse({'success': False, 'error': 'Faqat token URL orqali kiriş mumkin', 'use_token': True}, status=403)

    video = get_object_or_404(VideoLesson, pk=pk)
    _ensure_gating_duration(video)
    return _complete_video_watch(request, video)


def _notify_ai_graded(submission, ai_result):
    """AI baholaganda öquvçi, guruh va adminlarga xabar"""
    student = submission.student
    profession = submission.homework.lesson.profession.name
    lesson = submission.homework.lesson.title
    grade = ai_result['grade']
    feedback = ai_result['feedback']

    # Saytda şaxsiy xabar
    Message.objects.create(
        recipient=student,
        title=f"⏰ Tahrirlaş muddati tugadi — vazifa baholandi",
        content=f"'{lesson}' vazifangiz uçun 1 soatlik tahrirlaş muddati tugadi.\n\n🏆 Baho: {grade}/10\n\n💬 AI izohi:\n{feedback}",
        message_type='system'
    )

    notify_user(
        student.telegram_id,
        f"⏰ <b>Tahrirlaş muddati tugadi!</b>\n"
        f"✅ <b>Vazifangiz AI tomonidan baholandi!</b>\n\n"
        f"📚 {profession}\n"
        f"📖 {lesson}\n"
        f"🏆 Baho: <b>{grade}/10</b>\n\n"
        f"💬 <b>AI izohi:</b>\n{feedback}"
    )
    notify_admins(
        f"✅ <b>Uy vazifasi AI baholadi!</b>\n\n"
        f"👤 {student.first_name} {student.last_name}\n"
        f"📚 {profession} / {lesson}\n"
        f"🏆 Baho: <b>{grade}/10</b>\n"
        f"💬 {feedback[:200]}"
    )


def _notify_ai_revision(submission, ai_result):
    """AI qayta işlaşga yuborganda öquvçi, guruh va adminlarga xabar"""
    student = submission.student
    profession = submission.homework.lesson.profession.name
    lesson = submission.homework.lesson.title
    grade = ai_result['grade']
    feedback = ai_result['feedback']

    # Saytda şaxsiy xabar
    Message.objects.create(
        recipient=student,
        title=f"⏰ Tahrirlaş muddati tugadi — vazifa qayta işlaş kerak",
        content=f"'{lesson}' vazifangiz uçun 1 soatlik tahrirlaş muddati tugadi.\n\n🏆 Baho: {grade}/10\n\n💬 Sabab:\n{feedback}\n\nIltimos, tuzatib qayta yuboring!",
        message_type='warning'
    )

    notify_user(
        student.telegram_id,
        f"⏰ <b>Tahrirlaş muddati tugadi!</b>\n"
        f"⚠️ <b>Vazifangiz qayta işlaş kerak!</b>\n\n"
        f"📚 {profession}\n"
        f"📖 {lesson}\n"
        f"🏆 Baho: <b>{grade}/10</b>\n\n"
        f"💬 <b>Sabab:</b>\n{feedback}\n\n"
        f"📝 Iltimos, tuzatib qayta yuboring!"
    )
    notify_admins(
        f"⚠️ <b>Uy vazifasi AI qaytardi!</b>\n\n"
        f"👤 {student.first_name} {student.last_name}\n"
        f"📚 {profession} / {lesson}\n"
        f"🏆 Baho: <b>{grade}/10</b>\n"
        f"💬 {feedback[:200]}"
    )


def _schedule_ai_check(submission_pk):
    """Öquvçi vazifa yuborgandan 1 soat keyin AI tekşiruvini rejalaştiriş"""
    import threading

    def _run_ai_check(sub_pk):
        from django import db
        from accounts.models import HomeworkSubmission
        from coin.models import ActivityLog

        try:
            sub = HomeworkSubmission.objects.select_related(
                'homework__lesson__profession', 'student'
            ).get(pk=sub_pk)
        except HomeworkSubmission.DoesNotExist:
            return
        finally:
            db.close_old_connections()

        if sub.ai_checked or sub.status != 'pending':
            return

        hw = sub.homework
        student_answer = _read_submission_content(sub)

        if not student_answer:
            if sub.submission_type == 'file' and sub.file:
                import os
                if os.path.splitext(sub.file.name)[1].lower() == '.pdf':
                    sub.ai_feedback = "📄 Suniy Intellekt PDF formatni qöllab-quvvatlamaydi. Faqat matn yoki kod fayllarini tekşira oladi."
            sub.ai_checked = True
            sub.save(update_fields=['ai_checked', 'ai_feedback'])
            return

        try:
            ai_result = _get_ai_grade(hw.description, student_answer)
            if ai_result:
                sub.grade = ai_result['grade']
                sub.status = ai_result['status']
                sub.graded_at = timezone.now()
                feedback_text = f"🤖 AI xulosasi:\n{ai_result['feedback']}"
                sub.ai_feedback = feedback_text
                sub.feedback = feedback_text
                sub.ai_checked = True
                sub.save(update_fields=[
                    'grade', 'status', 'graded_at',
                    'ai_feedback', 'feedback', 'ai_checked'
                ])

                if ai_result['status'] == 'graded' and not sub.coin_awarded:
                    coin_amount = sub.calculate_coin_reward(ai_result['grade'])
                    sub.student.add_coins(coin_amount, f"Vazifa baholandi: {hw.lesson.title}")
                    sub.coin_awarded = True
                    sub.coins_earned = coin_amount
                    sub.save(update_fields=['coin_awarded', 'coins_earned'])
                    ActivityLog.objects.create(
                        user=sub.student, action_type='homework_graded',
                        description=f"Vazifa AI baholadi: {hw.lesson.title} - Baho: {ai_result['grade']}, +{coin_amount} coin"
                    )
                    _notify_ai_graded(sub, ai_result)
                elif ai_result['status'] == 'revision':
                    _notify_ai_revision(sub, ai_result)
            else:
                sub.ai_checked = True
                sub.save(update_fields=['ai_checked'])
        except Exception as e:
            print(f"AI auto-check error (submission #{sub_pk}): {e}")
        finally:
            db.close_old_connections()

    timer = threading.Timer(3600, _run_ai_check, args=[submission_pk])
    timer.daemon = True
    timer.start()


def _notify_homework_submitted(student, homework):
    """Yangi vazifa yuborilganda faqat kurs öqituvçilariga xabar"""
    hw_text = homework.description[:50] if homework.description else ""
    profession = homework.lesson.profession
    lesson = homework.lesson.title

    # Faqat şu kursga biriktirilgan öqituvçilarga xabar yuboriş
    teachers = profession.teachers.all()
    for teacher in teachers:
        # Saytda şaxsiy xabar
        Message.objects.create(
            recipient=teacher,
            title=f"📝 Yangi uy vazifasi: {lesson}",
            content=f"{student.first_name} {student.last_name} '{profession.name}' kursining '{lesson}' darsiga uy vazifasini yubordi. Tekşirişni kutmoqda.",
            message_type='personal'
        )
        # Telegram xabar
        if teacher.telegram_id:
            notify_user(
                teacher.telegram_id,
                f"📝 <b>Yangi uy vazifasi yuborildi!</b>\n\n"
                f"👤 {student.first_name} {student.last_name}\n"
                f"📚 Kurs: {profession.name}\n"
                f"📖 Dars: {lesson}\n"
                f"📋 {hw_text}\n\n"
                f"👨‍🏫 Tekşirişni unutmang!"
            )

    # Agar kursga öqituvçi biriktirilmagan bölsa, adminlarga xabar
    if not teachers.exists():
        notify_admins(
            f"📝 <b>Yangi uy vazifasi yuborildi!</b>\n\n"
            f"👤 {student.first_name} {student.last_name}\n"
            f"📚 Kurs: {profession.name}\n"
            f"📖 Dars: {lesson}\n"
            f"📋 {hw_text}\n\n"
            f"⚠️ Bu kursga öqituvçi biriktirilmagan!"
        )


def _notify_homework_graded(grader, submission, grade):
    """Vazifa baholanganda öquvçi, guruh va adminlarga xabar"""
    student = submission.student
    profession = submission.homework.lesson.profession.name
    lesson = submission.homework.lesson.title
    feedback = submission.feedback or ""

    notify_user(
        student.telegram_id,
        f"✅ <b>Uy vazifangiz baholandi!</b>\n\n"
        f"📚 Kurs: {profession}\n"
        f"📖 Dars: {lesson}\n"
        f"📊 Baho: <b>{grade}</b> ball\n"
        f"👨‍🏫 Baholadi: {grader.first_name} {grader.last_name}\n"
        f"{'💬 Izoh: ' + feedback[:100] if feedback else ''}"
    )
    notify_admins(
        f"✅ <b>Uy vazifasi baholandi!</b>\n\n"
        f"👤 {student.first_name} {student.last_name}\n"
        f"📚 {profession} → {lesson}\n"
        f"📊 Baho: {grade} ball\n"
        f"👨‍🏫 {grader.first_name} {grader.last_name}"
    )


def _normalize_code(text):
    """Kodni normallaştiriş — böş joylar, izohlar, katta-kichik harflarni tenglaştiriş"""
    import re
    lines = text.splitlines()
    cleaned = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Izohlarni olib taşlaş
        line = re.sub(r'#.*$', '', line).strip()
        line = re.sub(r'//.*$', '', line).strip()
        if not line:
            continue
        # Böş joylarni birlaştiriş, kiçik harfga
        line = re.sub(r'\s+', ' ', line).lower()
        cleaned.append(line)
    return '\n'.join(cleaned)


SIMILARITY_MAX_CHARS = 6000


def _calculate_similarity(text1, text2):
    """Ikki matnni soliştiriş — SequenceMatcher + token-based"""
    from difflib import SequenceMatcher
    import re

    if not text1 or not text2:
        return 0

    norm1 = _normalize_code(text1)
    norm2 = _normalize_code(text2)

    if not norm1 or not norm2:
        return 0

    # Qisqa kodlarni plagiat deb belgilamaslik (10 qatordan kam = oddiy vazifa)
    lines1 = norm1.splitlines()
    lines2 = norm2.splitlines()
    min_lines = min(len(lines1), len(lines2))
    if min_lines < 5:
        return 0

    # 1. SequenceMatcher — umumiy öxşaşlik. Algoritm kvadratik: katta fayllarda
    # soniyalab CPU ni band qilib (GIL), şu jarayondagi boşqa sörovlarni sekinlatardi.
    seq_ratio = SequenceMatcher(None, norm1[:SIMILARITY_MAX_CHARS], norm2[:SIMILARITY_MAX_CHARS]).ratio() * 100

    # 2. Qator-böyicha taqqoslaş — NOYOB qatorlar (print("salom") kabi umumiy qatorlarni filtrlaş)
    common_patterns = {
        'print(', 'input(', 'if ', 'else:', 'elif ', 'for ', 'while ',
        'def ', 'return', 'import ', 'class ', 'pass', 'break', 'continue',
        'true', 'false', 'none', '= int(', '= str(', '= float(',
    }
    set1 = set(lines1)
    set2 = set(lines2)
    # Umumiy pattern bölgan qatorlarni çiqarib taşlaş
    unique1 = {l for l in set1 if not any(p in l for p in common_patterns)}
    unique2 = {l for l in set2 if not any(p in l for p in common_patterns)}
    if unique1 and unique2:
        common = unique1 & unique2
        total = max(len(unique1), len(unique2))
        line_ratio = (len(common) / total) * 100
    else:
        line_ratio = 0

    # 3. Token-based
    tokens1 = re.findall(r'[a-zA-Z_]\w*|[^\s\w]', norm1)
    tokens2 = re.findall(r'[a-zA-Z_]\w*|[^\s\w]', norm2)
    if tokens1 and tokens2:
        token_ratio = SequenceMatcher(None, tokens1, tokens2).ratio() * 100
    else:
        token_ratio = 0

    # Örtaça oliş (max emas!) — bitta yuqori çiqsa ham boşqalari pasaytiradi
    similarity = (seq_ratio + line_ratio + token_ratio) / 3

    # Qisqa kodlarda similarity ni pasaytiriş (10 qatordan kam bölsa 20% kamaytiriş)
    if min_lines < 10:
        similarity *= 0.8

    return round(similarity, 1)


def _check_similarity_with_others(homework, current_submission, student_answer):
    """Boşqa öquvçilarning javoblari bilan öxşaşlikni tekşiriş — lokal algoritm"""

    other_submissions = HomeworkSubmission.objects.filter(
        homework=homework
    ).exclude(
        pk=current_submission.pk
    ).exclude(
        status='revision'
    ).select_related('student').order_by('-submitted_at')[:30]

    if not other_submissions:
        return None

    best_match = {'similarity': 0, 'student': '', 'student_id': None}

    for sub in other_submissions:
        content = _read_submission_content(sub)
        if not content or len(content.strip()) <= 20:
            continue

        sim = _calculate_similarity(student_answer, content)

        if sim > best_match['similarity']:
            best_match = {
                'similarity': sim,
                'student': sub.student.full_name if hasattr(sub.student, 'full_name') else f"{sub.student.first_name} {sub.student.last_name}",
                'student_id': sub.student.pk,
            }

    if best_match['similarity'] < 85:
        return {
            'is_plagiat': False,
            'similarity': int(best_match['similarity']),
            'similar_to': best_match['student'],
            'reason': '',
        }

    sim_pct = int(best_match['similarity'])
    if sim_pct >= 95:
        reason = f"Javob {best_match['student']} ning javobiga deyarli bir xil (faqat böş joylar yoki özgaruvçi nomlari farq qiladi)"
    elif sim_pct >= 90:
        reason = f"Javobning asosiy tuzilişi va mantiqi {best_match['student']} ning javobi bilan juda öxşaş"
    else:
        reason = f"Javob {best_match['student']} ning javobiga öxşaş körinadi"

    return {
        'is_plagiat': True,
        'similarity': sim_pct,
        'similar_to': best_match['student'],
        'reason': reason,
    }


def _get_student_writing_profile(student, current_submission_pk=None):
    """Öquvçining oldingi qabul qilingan javoblaridan yoziş uslubini yiğiş"""
    previous_subs = HomeworkSubmission.objects.filter(
        student=student,
        status__in=['accepted', 'graded'],
    ).exclude(
        pk=current_submission_pk
    ).order_by('-submitted_at')[:10]

    previous_answers = []
    for sub in previous_subs:
        content = _read_submission_content(sub)
        if content and len(content.strip()) > 20:
            snippet = content[:800] if len(content) > 800 else content
            previous_answers.append(snippet)
        if len(previous_answers) >= 5:
            break

    return previous_answers


def _get_student_academic_context(student, homework):
    """Öquvçining öquv faoliyatini yiğiş — AI adolatli qaror qabul qilişi uçun"""
    ctx = {
        'avg_grade': None,
        'total_graded': 0,
        'is_strong_student': False,
        'revision_submissions_count': 0,
        'has_revision_history': False,
        'course_passed_quizzes': 0,
    }

    # Umumiy örtaça baho (oxirgi 30 ta baholangan vazifa)
    graded_grades = list(
        HomeworkSubmission.objects.filter(
            student=student,
            status__in=['graded', 'accepted'],
            grade__isnull=False,
        ).values_list('grade', flat=True)[:30]
    )
    if graded_grades:
        ctx['avg_grade'] = round(sum(graded_grades) / len(graded_grades), 1)
        ctx['total_graded'] = len(graded_grades)
        ctx['is_strong_student'] = ctx['avg_grade'] >= 7.0

    # Qayta işlaş tarixi — köp marta qayta işlagan öquvçi odatda halolroq
    rev_count = HomeworkSubmission.objects.filter(
        student=student, revision_count__gt=0
    ).count()
    ctx['revision_submissions_count'] = rev_count
    ctx['has_revision_history'] = rev_count > 0

    # Kurs böyiça ötilgan testlar soni
    try:
        from quiz.models import QuizAttempt
        ctx['course_passed_quizzes'] = QuizAttempt.objects.filter(
            student=student,
            quiz__lesson__profession=homework.lesson.profession,
            passed=True,
        ).count()
    except Exception:
        pass

    return ctx


def _notify_ai_penalty(submission, penalty_coins, suspicion_reasons):
    """AI jarima qöyganda öquvçiga saytda xabar va Telegram orqali bildiriş"""
    student = submission.student
    lesson = submission.homework.lesson.title
    profession = submission.homework.lesson.profession.name

    reasons_text = '\n'.join(f"• {r}" for r in suspicion_reasons)
    penalty_line = f"\n💸 Jarima: -{penalty_coins} coin ayirildi." if penalty_coins > 0 else ""

    content = (
        f"⚠️ '{lesson}' darsiga uy vazifangizda şubhali belgilar aniqlandi.\n\n"
        f"🔍 Sabab:\n{reasons_text}"
        f"{penalty_line}\n\n"
        f"📝 Vazifangiz qayta işlaşga qaytarildi. "
        f"Iltimos, vazifani ÖZINGIZ bajaring va qayta yuboring!\n\n"
        f"💡 Eslatma: Özingizning tuşunişingiz eng muhim — AI nusxasi emas."
    )

    Message.objects.create(
        recipient=student,
        title="⚠️ Vazifada şubhali belgilar — qayta işlaş kerak",
        content=content,
        message_type='warning',
    )

    tg_penalty = f"\n💸 <b>Jarima:</b> -{penalty_coins} coin ayirildi" if penalty_coins > 0 else ""
    notify_user(
        student.telegram_id,
        f"⚠️ <b>Vazifada şubhali belgilar aniqlandi!</b>\n\n"
        f"📚 {profession}\n"
        f"📖 {lesson}\n\n"
        f"🔍 <b>Sabab:</b>\n{reasons_text}"
        f"{tg_penalty}\n\n"
        f"📝 Vazifangiz qayta işlaşga qaytarildi. ÖZINGIZ bajaring!"
    )


def _check_ai_generated(homework_description, student_answer, student=None, current_submission_pk=None, academic_context=None):
    """Javob suniy intellekt yordamida yozilganligini tekşiriş — QATTIQ REJIM + öquvçi profili"""
    import json
    from django.conf import settings
    api_key = getattr(settings, 'OPENAI_API_KEY', None)
    if not api_key or not api_key.strip():
        return None

    # Öquvçining oldingi javoblarini yiğiş
    previous_answers = []
    if student:
        previous_answers = _get_student_writing_profile(student, current_submission_pk)

    # Oldingi javoblar bölimini tayyorlaş
    profile_section = ""
    if previous_answers:
        profile_section = (
            "\n\n📊 ÖQUVÇINING OLDINGI QABUL QILINGAN JAVOBLARI (uslub namunasi):\n"
            "Bu javoblar öqituvçi tomonidan tekşirilgan va qabul qilingan — bu öquvçining HAQIQIY yoziş uslubi.\n"
            "Hozirgi javobni ŞU uslub bilan soliştiring!\n\n"
        )
        for i, ans in enumerate(previous_answers, 1):
            profile_section += f"--- Oldingi javob #{i} ---\n{ans}\n\n"
        profile_section += (
            "🔑 MUHIM TAHLIL QOIDALARI:\n"
            "- Agar hozirgi javob oldingi javoblardan KESKIN FARQ QILSA (uslub, murakkablik, til, sifat) → bu AI belgisi!\n"
            "- Agar öquvçi oldin oddiy kod yozgan, endi birdan professional darajada yozsa → AI!\n"
            "- Agar öquvçi oldin özbekça özgaruvçi işlatgan, endi professional inglizça → AI!\n"
            "- Agar öquvçi oldin izohsiz yozgan, endi har qatorga izoh yozgan → AI!\n"
            "- Agar öquvçi oldin xatolar qilgan, endi mukammal kod yozgan → AI!\n"
            "- Agar uslub öxşaş bölsa va tabiiy rivojlaniş körinsa → bu AI EMAS\n"
        )
    else:
        profile_section = (
            "\n\n⚠️ Bu öquvçining BIRINÇI topşiriği — oldingi javoblar mavjud emas.\n"
            "Şuning uçun faqat AI belgilariga qarab tekşiring.\n"
        )

    # Öquvçining akademik faoliyati bölimi
    academic_section = ""
    is_strong = False
    if academic_context:
        lines = []
        total = academic_context.get('total_graded', 0)
        avg = academic_context.get('avg_grade')
        if total > 0 and avg is not None:
            lines.append(f"- Öquvçi örtaça bahosi: {avg}/10 ({total} ta baholangan vazifa)")
            if academic_context.get('is_strong_student'):
                is_strong = True
                lines.append("- ⭐ Bu KUÇLI öquvçi — u öz bilimi bilan yaxşi yozişi mumkin!")
        rev = academic_context.get('revision_submissions_count', 0)
        if academic_context.get('has_revision_history') and rev > 0:
            lines.append(f"- Bu öquvçi ilgari {rev} marta qayta işlagan — bu HALOLLIK belgisi!")
        quizzes = academic_context.get('course_passed_quizzes', 0)
        if quizzes > 0:
            lines.append(f"- Kurs böyiça {quizzes} ta testdan ötgan (mavzuni biladi!)")
        if lines:
            academic_section = (
                "\n\n📈 ÖQUVÇINING ÖQUV FAOLIYATI (adolatli baholaş uçun):\n"
                + "\n".join(lines)
                + "\n🔑 Agar öquvçi kuçli va tarix halollikni körsatsa — işonç çegarasini OŞIRING!"
            )

    # Kuçli öquvçilar uçun işonç çegarasini oşiriş (false positive kamaytiriş)
    confidence_note = ""
    if is_strong:
        confidence_note = (
            "\n⭐ DIQQAT: Bu kuçli öquvçi (avg >= 7). "
            "Uning uçun is_ai=true deyiş uçun confidence >= 85 bölişi KERAK (odatdagi 75 emas)!\n"
        )

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key.strip(), timeout=60)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Siz öquv platformasining AI tekşiruvçisisiz. "
                        "Boşlanğiç dasturlaş öquvçilarining javoblarini tekşirasiz.\n\n"
                        "🛡️ AYBSIZLIK PREZUMPSIYASI: Öquvçi standart holatda HALOL deb qabul qilinadi. "
                        "Faqat 4+ ta ANIQ AI belgisi bir vaqtda bölgandagina flag qöying. "
                        "Bitta yoki ikki belgi — bu AI EMAS. Şubha bölsa — HAR DOIM öquvçi foydasiga hal qiling.\n"
                        "⚠️ Notöğri flag qöyiş öquvçini nohaqdan jarimaga tortadi va öqiş motivatsiyasini yöq qiladi!\n"
                        f"{confidence_note}\n"
                        "🔍 AI deb belgilaş uçun quyidagi belgilardan KAMIDA 4 TASI bir vaqtda bölişi kerak:\n"
                        "1. AI IBORALARI: 'Here is', 'Let me', 'certainly', 'I'd be happy to', "
                        "'Let's break this down', 'Note that', 'Feel free', 'In this solution' iboralari\n"
                        "2. CHATGPT FORMATI: Markdown (###, **bold**), qadam-va qadam raqamlangan tuşuntiriş, "
                        "kod bloki taşqarisidagi mukammal tuzilgan xatboşi\n"
                        "3. PROFESSIONAL INGLIZÇA IZOHLAR: Deyarli HAR QATORGA mukammal grammatik "
                        "inglizça comment yozilgan (kiçik xatosiz)\n"
                        "4. COPY-PASTE BELGILARI: Vazifaga umuman aloqasi yöq import, funksiyalar yoki "
                        "boşqa tildagi bölaklar mavjud\n"
                        "5. TÖLIQ KÖÇIRMA: Javob aniq körinib turgan tarzda AI'dan olingan "
                        "(masalan, 'Sure! Here is a Python solution...' bilan boşlanadi)\n"
                        "6. USLUB SAKRAŞI: Oldingi javoblariga qaraganda til, murakkablik va sifat "
                        "sezilarli darajada YUQORI, tabiiy rivojlaniş sifatida tuşuntirib bölmaydigan\n\n"
                        "✅ Bu AI EMAS — HAQIQIY öquvçi belgilari (BULARNI AI DEB BELGILAMANG):\n"
                        "- Öquvçi yaxshi/toza kod yozgan — örgangan yoki qobiliyatli\n"
                        "- Javob qisqa va töğri — bu AI degani emas, aksinça oddiy va tabiiy\n"
                        "- try/except, class, list comprehension işlatgan — darsda örgangan bölişi mumkin\n"
                        "- Özbekça yoki inglizça izohlar yozgan — bu yaxşi amaliyot\n"
                        "- Oldingi javoblardan yaxşiroq yozgan — bu TABIIY rivojlaniş\n"
                        "- Kod stackoverflow'dan öxşab körinadi — bu keng tarqalgan pattern bölişi mumkin\n"
                        "- Kiçik xatolar, typo, notekis formatlaş bor — HAQIQIY öquvçi belgisi\n"
                        "- Özbekça özgaruvçi nomlari işlatgan — öquvçining odati\n"
                        "- f-string, dictionary, tuple işlatgan — dasturlaşning oddiy vositalari\n"
                        "- Kod umumiy öxşaş, chunki vazifada bitta töğri yeçim bor\n\n"
                        "Faqat JSON formatda javob bering:\n"
                        '{"is_ai": true/false, "confidence": <0-100>, "reason": "<özbek tilida sabab>", '
                        '"style_match": "<oldingi javoblar bilan solishtirish>"}\n\n'
                        "CONFIDENCE QOIDALARI (juda muhim):\n"
                        "- 0–60:  is_ai=false — belgi yöq yoki juda kam\n"
                        "- 61–84: is_ai=false — şubha bor, LEKIN aniq emas. Bu doim FALSE bölişi kerak\n"
                        "- 85–94: is_ai=true — kuçli şubha bor, 4+ belgi mavjud\n"
                        "- 95–100: is_ai=true — 5–6 ta belgi bilan yaqqol AI\n"
                        "- Ikkilanayotgan bölsangiz — confidence ni PAST qiling va is_ai=false qöying\n"
                        "- reason: Özbek tilida qisqa izoh\n"
                        "- style_match: Oldingi javoblar bilan soliştiriş"
                    )
                },
                {
                    "role": "user",
                    "content": (
                        f"Vazifa tavsifi:\n{homework_description}\n\n"
                        f"Öquvçining hozirgi javobi:\n{student_answer}"
                        f"{profile_section}"
                        f"{academic_section}"
                    )
                }
            ],
            max_tokens=500,
            temperature=0.05,
        )
        result_text = response.choices[0].message.content.strip()
        if result_text.startswith('```'):
            result_text = result_text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        result = json.loads(result_text)
        reason = result.get('reason', '')
        style_match = result.get('style_match', '')
        if style_match:
            reason = f"{reason}\n📊 Uslub tahlili: {style_match}"
        return {
            'is_ai': bool(result.get('is_ai', False)),
            'confidence': min(100, max(0, int(result.get('confidence', 0)))),
            'reason': reason,
        }
    except Exception as e:
        print(f"AI check error: {e}")
        return None


def _get_ai_homework_feedback(homework_description, student_answer):
    """Premium foydalanuvçilar uçun AI xulosasi oliş"""
    from django.conf import settings
    api_key = getattr(settings, 'OPENAI_API_KEY', None)
    if not api_key or not api_key.strip():
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key.strip(), timeout=30)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Siz öquv platformasining AI tekşiruvçisisiz. Özbek tilida javob bering. "
                        "Öquvçining uyga vazifasini tekşiring va qisqa xulosa bering:\n"
                        "1. Javob töğri yoki notöğri ekanligini ayting\n"
                        "2. Qisqa tuşuntiriş bering (3-5 gap)\n"
                        "3. Agar xato bölsa, töğri yönalişni körsating\n"
                        "4. Rağbatlantiruvçi ohangda yozing\n"
                        "Javobni 150 sözdan oşirmang."
                    )
                },
                {
                    "role": "user",
                    "content": (
                        f"Vazifa tavsifi:\n{homework_description}\n\n"
                        f"Öquvçining javobi:\n{student_answer}"
                    )
                }
            ],
            max_tokens=500,
            temperature=0.3,
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"AI homework feedback error: {e}")
        return None


def _get_ai_grade(homework_description, student_answer):
    """AI yordamida vazifani baholaş: ball, status va izoh qaytaradi"""
    import json
    from django.conf import settings
    api_key = getattr(settings, 'OPENAI_API_KEY', None)
    if not api_key or not api_key.strip():
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key.strip(), timeout=45)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Siz öquv platformasining AI baholovçisisiz. Öquvçining uyga vazifasini tekşiring va baholang.\n\n"
                        "Faqat JSON formatda javob bering, boşqa heç narsa yozmang:\n"
                        '{"grade": <1-10 orasida son>, "status": "<graded yoki revision>", "feedback": "<özbek tilida izoh>"}\n\n'
                        "Qoidalar:\n"
                        "- grade: 1 dan 10 gaça son. Vazifa sifatiga qarab adolatli baho qöying\n"
                        "- status: FAQAT javob böş, umuman notöğri yoki vazifaga UMUMAN aloqasi bölmasa 'revision' qöying\n"
                        "- status: agar javob qisman bölsa ham, töğri yönalişda bölsa 'graded' qöying\n"
                        "- feedback: Özbek tilida 2-4 gapda izoh yozing. Nimasi yaxşi, nimasi yomon, qanday yaxşilaş mumkinligini ayting\n"
                        "- Agar javob böş yoki umuman boğliq bölmasa: grade=1, status=revision\n"
                        "- Agar javob qisman töğri bölsa: grade=4-6, status=graded\n"
                        "- Agar javob yaxşi bölsa: grade=7-9, status=graded\n"
                        "- Agar javob mukammal bölsa: grade=10, status=graded\n\n"
                        "⚠️ MUHIM: 'revision' statusini JUDA KAM işlating! Faqat javob BUTUNLAY notöğri bölganda.\n"
                        "Agar öquvçi harakat qilgan va biror narsa yozgan bölsa — 'graded' qöying, past baho bering.\n"
                        "Har safar bir xil javobga BIR XIL baho bering — izçil böling!"
                    )
                },
                {
                    "role": "user",
                    "content": (
                        f"Vazifa tavsifi:\n{homework_description}\n\n"
                        f"Öquvçining javobi:\n{student_answer}"
                    )
                }
            ],
            max_tokens=500,
            temperature=0.1,
        )
        result_text = response.choices[0].message.content.strip()
        # JSON ni parse qiliş
        if result_text.startswith('```'):
            result_text = result_text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        result = json.loads(result_text)
        grade = max(1, min(10, int(result.get('grade', 5))))
        status = result.get('status', 'graded')
        if status not in ('graded', 'revision'):
            status = 'graded'
        feedback = result.get('feedback', '')
        return {'grade': grade, 'status': status, 'feedback': feedback}
    except Exception as e:
        print(f"AI grade error: {e}")
        return None


def _read_submission_content(submission):
    """Submission mazmunini öqiş (matn, fayl yoki rasm)"""
    if submission.submission_type == 'text' and submission.text_content:
        from .web_homework import decode_project, project_source
        project = decode_project(submission.text_content)
        if project is not None:
            return project_source(project)
        return submission.text_content
    elif submission.submission_type == 'file' and submission.file:
        import os
        file_ext = os.path.splitext(submission.file.name)[1].lower()
        CODE_EXTENSIONS = (
            '.py', '.js', '.html', '.css', '.java', '.c', '.cpp',
            '.json', '.xml', '.sql', '.txt', '.jsx', '.ts', '.tsx',
            '.php', '.rb', '.go', '.rs', '.swift', '.kt', '.sh',
            '.bat', '.ps1', '.yaml', '.yml', '.md', '.csv',
        )
        IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.gif', '.webp')
        if file_ext == '.pdf':
            return None
        elif file_ext in IMAGE_EXTENSIONS:
            return _read_image_with_ai(submission.file)
        elif file_ext in CODE_EXTENSIONS:
            try:
                submission.file.seek(0)
                content = submission.file.read().decode('utf-8', errors='replace')
                if len(content) > 5000:
                    content = content[:5000] + "\n... (qolgan qismi qisqartirildi)"
                return content
            except Exception:
                return None
    return None


def _read_image_with_ai(file_field):
    """Rasm faylni OpenAI Vision orqali öqib matn sifatida qaytariş"""
    import base64
    from django.conf import settings
    api_key = getattr(settings, 'OPENAI_API_KEY', None)
    if not api_key or not api_key.strip():
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key.strip(), timeout=30)
        file_field.seek(0)
        image_data = base64.b64encode(file_field.read()).decode('utf-8')
        import os
        ext = os.path.splitext(file_field.name)[1].lower().strip('.')
        if ext == 'jpg':
            ext = 'jpeg'
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Bu rasmdagi barça matn, kod yoki javoblarni aynan yoz. Faqat rasmdagi kontentni qaytar, boşqa heç narsa qöşma."},
                        {"type": "image_url", "image_url": {"url": f"data:image/{ext};base64,{image_data}"}}
                    ]
                }
            ],
            max_tokens=2000,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Image read error: {e}")
        return None


@login_required
def submit_homework(request, pk):
    homework = get_object_or_404(Homework, pk=pk)
    redir = _redirect_student_to_token(request, homework.lesson)
    if redir:
        return redir
    
    if request.method == 'POST':
        # Agar oldin topşirilgan bölsa, qayta yaratmaslik
        existing = HomeworkSubmission.objects.filter(homework=homework, student=request.user).first()
        if existing:
            messages.info(request, "Siz bu vazifani allaqaçon topşirgansiz!")
            return redirect('lesson_view', pk=homework.lesson.pk)
        
        submission_type = request.POST.get('submission_type', 'file')
        submission = None
        
        # Faqat fayl rejimida matn qabul qilinmaydi
        if homework.file_only:
            submission_type = 'file'
        
        # Yozma topşiriş rejimida faqat rasm fayl qabul qilinadi
        if homework.handwritten_only:
            submission_type = 'file'
            uploaded_file = request.FILES.get('file')
            if uploaded_file:
                import os
                ext = os.path.splitext(uploaded_file.name)[1].lower()
                if ext not in ('.jpg', '.jpeg', '.png'):
                    messages.error(request, "Faqat rasm formatida (.jpg, .jpeg, .png) qabul qilinadi!")
                    return redirect('lesson_view', pk=homework.lesson.pk)
        
        if submission_type == 'file':
            form = HomeworkSubmissionForm(request.POST, request.FILES)
            if form.is_valid():
                submission = form.save(commit=False)
                submission.homework = homework
                submission.student = request.user
                submission.submission_type = 'file'
                submission.save()
                messages.success(request, "Vazifa muvaffaqiyatli yuborildi!")
                _notify_homework_submitted(request.user, homework)
        else:
            text_content = request.POST.get('text_content', '').strip()
            if text_content:
                submission = HomeworkSubmission.objects.create(
                    homework=homework,
                    student=request.user,
                    submission_type='text',
                    text_content=text_content
                )
                messages.success(request, "Vazifa muvaffaqiyatli yuborildi!")
                _notify_homework_submitted(request.user, homework)
            else:
                messages.error(request, "Matn kiritişingiz kerak!")
        
        # AI tekşiruv — 1 soat tahrirlaş muddati tugagandan keyin avtomatik işlaydi
        if submission and homework.ai_check and homework.lesson.profession.ai_check_enabled:
            _schedule_ai_check(submission.pk)
    
    return redirect('lesson_view', pk=homework.lesson.pk)


@login_required
def edit_homework(request, pk):
    """Topşiriqni 1 soat içida tahrirlaş"""
    submission = get_object_or_404(HomeworkSubmission, pk=pk, student=request.user)
    redir = _redirect_student_to_token(request, submission.homework.lesson)
    if redir:
        return redir
    
    if not submission.can_edit:
        messages.error(request, "Tahrirlaş muddati tugagan (1 soat)!")
        return redirect('lesson_view', pk=submission.homework.lesson.pk)
    
    if request.method == 'POST':
        submission_type = request.POST.get('submission_type', submission.submission_type)
        
        if submission_type == 'file':
            new_file = request.FILES.get('file')
            if new_file:
                submission.file = new_file
                submission.submission_type = 'file'
                submission.save()
                messages.success(request, "Topşiriq muvaffaqiyatli tahrirlandi!")
            else:
                messages.error(request, "Fayl tanlang!")
        else:
            text_content = request.POST.get('text_content', '').strip()
            if text_content:
                submission.text_content = text_content
                submission.submission_type = 'text'
                submission.save()
                messages.success(request, "Topşiriq muvaffaqiyatli tahrirlandi!")
            else:
                messages.error(request, "Matn kiriting!")
    
    return redirect('lesson_view', pk=submission.homework.lesson.pk)


@login_required
def start_test(request, pk):
    test = get_object_or_404(Test, pk=pk)
    redir = _redirect_student_to_token(request, test.lesson)
    if redir:
        return redir
    
    profession = test.lesson.profession
    
    # Check if already taken and retry not allowed
    existing_result = TestResult.objects.filter(test=test, student=request.user).first()
    if existing_result:
        if test.test_type == 'final':
            # Yakuniy test: qayta işlaş uçun tasdiqlangan tölov kerak
            approved_payment = FinalTestPayment.objects.filter(
                student=request.user, test=test, status='approved'
            ).first()
            if not approved_payment:
                messages.info(request, "Yakuniy testni qayta işlaş uçun tölov qilişingiz kerak (65,000 söm).")
                return redirect('final_test_payment', pk=test.pk)
            # Tölovni işlatilgan deb belgilaş
            approved_payment.delete()
        elif not test.allow_retry:
            messages.info(request, "Siz bu testni allaqaçon topşirgansiz.")
            return redirect('test_result', pk=existing_result.pk)
    
    # Sessiya kalitlari
    sq_key = f'test_{test.pk}_questions'
    st_key = f'test_{test.pk}_start'
    sa_key = f'test_{test.pk}_answer_order'
    token_key = f'test_{test.pk}_submit_token'
    
    # Davom etayotgan test bormi? (sahifa yangilanganda qayta aralaştirmaslik)
    active_question_ids = request.session.get(sq_key)
    submit_token = request.session.get(token_key)
    
    if active_question_ids and not submit_token:
        # Test hali davom etmoqda — mavjud savollarni qayta yuklaş
        questions_qs = TestQuestion.objects.filter(
            pk__in=active_question_ids
        ).prefetch_related('answers')
        question_map = {q.pk: q for q in questions_qs}
        questions = [question_map[qid] for qid in active_question_ids if qid in question_map]
        
        # Javoblar tartibini tiklaş
        answer_order = request.session.get(sa_key, {})
        for q in questions:
            ordered_ids = answer_order.get(str(q.pk))
            if ordered_ids:
                answer_map = {a.pk: a for a in q.answers.all()}
                ordered_answers = [answer_map[aid] for aid in ordered_ids if aid in answer_map]
                q._prefetched_objects_cache['answers'] = ordered_answers
        
        # Qolgan vaqtni hisoblaş
        start_time_str = request.session.get(st_key)
        remaining = test.time_limit * 60
        if start_time_str:
            from datetime import datetime as dt
            try:
                started = dt.fromisoformat(start_time_str)
                elapsed = (timezone.now() - started).total_seconds()
                remaining = max(0, test.time_limit * 60 - int(elapsed))
            except (ValueError, TypeError):
                pass
        
        if remaining <= 0:
            # Vaqt tugagan — avtomatik yuboriş uçun formga yönaltiriş
            request.session.pop(sq_key, None)
            request.session.pop(st_key, None)
            request.session.pop(sa_key, None)
            return redirect('lesson_view', pk=test.lesson.pk)
        
        template = 'accounts/lessons/final_test_take.html' if test.test_type == 'final' else 'accounts/lessons/test_take.html'
        return render(request, template, {
            'test': test,
            'questions': questions,
            'remaining_time': remaining,
        })
    
    # ===== Yangi test boşlaş =====
    
    # Qayta topşiriş uçun coin yeçiş
    if existing_result and test.allow_retry and test.test_type != 'final':
        retry_cost = 5 if request.user.is_premium else 10
        request.user.remove_coins(retry_cost, f"Test qayta topşiriş: {test.lesson.title}")
    
    all_questions = list(test.questions.all().distinct().prefetch_related('answers'))
    
    import random
    
    # Savollar sonini çeklaş (question_count > 0 bölsa)
    if test.question_count and test.question_count > 0 and len(all_questions) > test.question_count:
        questions = random.sample(all_questions, test.question_count)
    else:
        questions = all_questions[:]
    
    if test.shuffle_questions:
        random.shuffle(questions)
    
    if test.shuffle_answers:
        for q in questions:
            answers_list = list(q.answers.all())
            random.shuffle(answers_list)
            q._prefetched_objects_cache['answers'] = answers_list
    
    # Sessiyaga saqlaş
    request.session[sq_key] = [q.pk for q in questions]
    request.session[st_key] = timezone.now().isoformat()
    request.session.pop(token_key, None)
    
    # Javoblar tartibini saqlaş
    answer_order = {}
    for q in questions:
        cached = getattr(q, '_prefetched_objects_cache', {}).get('answers')
        if cached is not None:
            answer_order[str(q.pk)] = [a.pk for a in cached]
        else:
            answer_order[str(q.pk)] = [a.pk for a in q.answers.all()]
    request.session[sa_key] = answer_order
    
    remaining = test.time_limit * 60
    
    template = 'accounts/lessons/final_test_take.html' if test.test_type == 'final' else 'accounts/lessons/test_take.html'
    return render(request, template, {
        'test': test,
        'questions': questions,
        'remaining_time': remaining,
    })


@login_required
def submit_test(request, pk):
    test = get_object_or_404(Test, pk=pk)
    redir = _redirect_student_to_token(request, test.lesson)
    if redir:
        return redir
    
    if request.method != 'POST':
        return redirect('start_test', pk=pk)
    
    # Takroriy yuborişni oldini oliş (POST refresh)
    submit_token = request.session.get(f'test_{test.pk}_submit_token')
    if submit_token:
        # Allaqaçon yuborilgan — oxirgi natijaga yönaltiriş
        last_result = TestResult.objects.filter(test=test, student=request.user).order_by('-id').first()
        if last_result:
            return redirect('test_result', pk=last_result.pk)
    request.session[f'test_{test.pk}_submit_token'] = True
    
    # Sessiyani tozalaş (qayta start_test ga kirganda yangi test boşlanişi uçun)
    request.session.pop(f'test_{test.pk}_answer_order', None)
    
    start_time = request.session.get(f'test_{test.pk}_start')
    started_at = timezone.now()
    if start_time:
        from datetime import datetime
        started_at = datetime.fromisoformat(start_time)
        
        # Server-side vaqt tekşiruvi — vaqt tugagan bölsa ham natijani qabul qiliş
        # lekin vaqt limitidan 60 sekund köp ötgan bölsa ogohlantiriş
        elapsed_seconds = (timezone.now() - started_at).total_seconds()
        allowed_seconds = test.time_limit * 60 + 60  # 60 sekund tolerantlik (network delay)
        if elapsed_seconds > allowed_seconds:
            messages.warning(request, "Vaqt çegarasi ötgan! Natijalar server vaqti bilan tekşirildi.")
    
    # Sessiyadan tanlangan savollarni oliş
    shown_question_ids = request.session.get(f'test_{test.pk}_questions')
    if shown_question_ids:
        questions = test.questions.filter(pk__in=shown_question_ids)
    else:
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
    
    # Birinçi urinişmi yoki qayta topşirişmi tekşiriş
    existing_results_count = TestResult.objects.filter(
        test=test, student=request.user
    ).exclude(pk=result.pk).count()
    is_first_attempt = existing_results_count == 0

    # Faqat birinçi urinişda: har bir töğri javob 5 coin + ötilsa 100 coin
    from .coin_rules import test_coins, test_coin_reason
    first_coins = test_coins(correct, result.passed) if is_first_attempt else 0
    if first_coins:
        request.user.add_coins(first_coins, test_coin_reason(test.lesson.title, correct, result.passed))
        result.coin_awarded = True
        result.save()

    if result.passed:
        # Activity log
        ActivityLog.objects.create(
            user=request.user,
            action_type='submit_test',
            description=f"Test topşirdi: {test.lesson.title} - {correct}/{total} töğri" + (f", +{first_coins} coin" if first_coins else "")
        )

        if first_coins:
            messages.success(request, f"Tabriklaymiz! Test muvaffaqiyatli! +{first_coins} coin oldingiz ({correct} ta töğri javob + ötganlik bonusi)!")
        else:
            messages.success(request, f"Tabriklaymiz! Test muvaffaqiyatli!")

        # 🎓 Yakuniy testdan ötdi — sertifikatni avtomatik beriş
        if test.test_type == 'final' and request.user.is_learner:
            try:
                from .certificates import issue_course_certificate
                _cert, _cert_err = issue_course_certificate(
                    request.user, test.lesson.profession
                )
                if _cert and _cert.file:
                    messages.success(
                        request,
                        "🎓 Tabriklaymiz! Sizga kurs sertifikati berildi — "
                        "kurs sahifasidan yuklab olişingiz mumkin!"
                    )
            except Exception:
                pass
    else:
        # Testdan ötmadi - faqat töğri javoblar uçun coin (birinçi urinişda)
        ActivityLog.objects.create(
            user=request.user,
            action_type='submit_test',
            description=f"Test topşirdi (muvaffaqiyatsiz): {test.lesson.title} - {correct}/{total} töğri" + (f", +{first_coins} coin" if first_coins else "")
        )
        messages.warning(request, f"Testdan öta olmadingiz. {correct}/{total} töğri javob.")
        if first_coins:
            messages.info(request, f"+{first_coins} coin oldingiz ({correct} ta töğri javob).")
    
    # Avtomatik xabar yuboriş
    from .notifications import on_test_completed
    on_test_completed(request.user, result)
    
    # Telegram xabarlar
    passed_text = "✅ Muvaffaqiyatli" if result.passed else "❌ Muvaffaqiyatsiz"
    notify_user(
        request.user.telegram_id,
        f"📊 <b>Test natijasi!</b>\n\n"
        f"📚 Kurs: {test.lesson.profession.name}\n"
        f"📖 Dars: {test.lesson.title}\n"
        f"✏️ Natija: {correct}/{total} töğri ({score}%)\n"
        f"{passed_text}"
    )

    # AJAX / fetch() bilan çaqirilgan bölsa — JSON qaytariş
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        from django.urls import reverse
        import json as _json
        redirect_url = reverse('test_result', kwargs={'pk': result.pk})
        return JsonResponse({
            'ok': True,
            'result_pk': result.pk,
            'redirect': redirect_url,
            'score': score,
            'passed': result.passed,
            'correct': correct,
            'total': total,
        })

    return redirect('test_result', pk=result.pk)


@login_required
def test_result(request, pk):
    result = get_object_or_404(TestResult, pk=pk)
    redir = _redirect_student_to_token(request, result.test.lesson)
    if redir:
        return redir
    
    user_answers = result.user_answers.all().select_related('question', 'selected_answer')
    
    lesson = result.test.lesson
    profession = lesson.profession
    section = lesson.section
    
    video_lesson = None
    homework_lesson = None
    same_section_lessons = Lesson.objects.filter(profession=profession, section=section) if section else Lesson.objects.filter(profession=profession)
    for l in same_section_lessons:
        if l.lesson_type == 'video' and not video_lesson:
            video_lesson = l
        elif l.lesson_type == 'homework' and not homework_lesson:
            homework_lesson = l
    
    return render(request, 'accounts/lessons/test_result.html', {
        'result': result,
        'user_answers': user_answers,
        'video_lesson': video_lesson,
        'homework_lesson': homework_lesson,
    })


# ===== STUDENT TOKEN-BASED VIEWS =====

def _get_token_or_404(user, token):
    return get_object_or_404(
        StudentLessonToken.objects.select_related(
            'lesson', 'lesson__profession', 'lesson__section',
        ),
        token=token,
        student=user,
    )


@login_required
def student_course_view(request, token):
    """Öquvçi uçun tokenli kurs sahifasi"""
    token_obj = get_object_or_404(
        StudentCourseToken.objects.select_related('profession'),
        token=token, student=request.user
    )
    profession = token_obj.profession

    if profession.is_blocked:
        messages.warning(request, f"'{profession.name}' kursi vaqtinça bloklangan. {profession.blocked_reason or ''}")
        return redirect('professions')

    is_enrolled = CourseEnrollment.objects.filter(user=request.user, profession=profession).exists() or request.user.profession == profession
    enrollment_request = None
    unenroll_request = None
    if is_enrolled:
        unenroll_request = CourseUnenrollRequest.objects.filter(user=request.user, profession=profession, status='pending').first()
    else:
        enrollment_request = CourseEnrollmentRequest.objects.filter(user=request.user, profession=profession).first()

    sections = []
    lessons_without_section = []
    lesson_progress = {}
    daily_section_today = None
    daily_limit_section_name = None
    section_daily_locked = {}
    lesson_is_locked = {}
    daily_lesson_today = None
    token_map = {}
    classmates = []
    course_teachers = profession.teachers.all() if hasattr(profession, 'teachers') else []
    course_completed = False
    has_empty_section = False
    certificate = None
    section_is_locked = {}

    if is_enrolled:
        StudentLessonToken.ensure_for_profession(request.user, profession)
        token_map = {
            t.lesson_id: t
            for t in StudentLessonToken.objects.filter(
                student=request.user, lesson__profession=profession
            )
        }
        sections = profession.sections.filter(is_active=True).prefetch_related('lessons')
        lessons_without_section = profession.lessons.filter(section__isnull=True)

        # Video progress
        watched_videos = VideoProgress.objects.filter(
            user=request.user, watched=True, video__lesson__profession=profession
        ).values_list('video__lesson_id', flat=True)
        for vid in watched_videos:
            lesson_progress[vid] = {'completed': True, 'type': 'video'}

        # Test results
        test_results = TestResult.objects.filter(
            student=request.user, test__lesson__profession=profession
        ).order_by('test__lesson_id', '-completed_at')
        latest_tests = {}
        for tr in test_results:
            if tr.test.lesson_id not in latest_tests:
                latest_tests[tr.test.lesson_id] = tr
        for lesson_id, tr in latest_tests.items():
            lesson_progress[lesson_id] = {
                'completed': tr.passed,
                'type': 'test',
                'score': tr.score,
                'passed': tr.passed,
                'attempted': True,
            }

        # Yangi quizlar uçun Attempt natijasini qöşiş (TestResult bölmagan darslar)
        from quiz.models import Attempt as QuizAttempt
        quiz_attempts = QuizAttempt.objects.filter(
            student=request.user,
            quiz__lesson__profession=profession,
            status__in=[QuizAttempt.Status.SUBMITTED, QuizAttempt.Status.GRADED],
        ).select_related('quiz').order_by('quiz__lesson_id', '-started_at')
        for qa in quiz_attempts:
            lid = qa.quiz.lesson_id
            if lid and lid not in lesson_progress:
                lesson_progress[lid] = {
                    'completed': qa.passed,
                    'type': 'test',
                    'passed': qa.passed,
                    'score': qa.score,
                    'attempted': True,
                }

        # Homework submissions
        hw_submissions = HomeworkSubmission.objects.filter(
            student=request.user, homework__lesson__profession=profession
        ).order_by('homework__lesson_id', '-submitted_at')
        latest_hw = {}
        for hw in hw_submissions:
            if hw.homework.lesson_id not in latest_hw:
                latest_hw[hw.homework.lesson_id] = hw
        # Topşirilgan vazifa darslari — test blokidan çiqariş uçun
        submitted_hw_lesson_ids = set(latest_hw.keys())
        for lesson_id, hw in latest_hw.items():
            is_completed = hw.status in ('accepted', 'graded')
            lesson_progress[lesson_id] = {
                'completed': is_completed,
                'type': 'homework',
                'graded': hw.grade is not None,
                'status': hw.status,
                'attempted': True,
            }

        # Speaking submissions
        speaking_submissions = SpeakingSubmission.objects.filter(
            student=request.user, speaking_task__lesson__profession=profession
        ).order_by('speaking_task__lesson_id', '-submitted_at')
        latest_speaking = {}
        for sp in speaking_submissions:
            if sp.speaking_task.lesson_id not in latest_speaking:
                latest_speaking[sp.speaking_task.lesson_id] = sp
        for lesson_id, sp in latest_speaking.items():
            is_completed = sp.status in ('graded',)
            lesson_progress[lesson_id] = {
                'completed': is_completed,
                'type': 'speaking',
                'graded': sp.grade is not None,
                'status': sp.status,
                'attempted': True,
            }

        # Daily section access
        from datetime import date
        today = date.today()
        limit_on = daily_limit_active(request.user, profession)
        accessed_section_ids = set(
            DailySectionAccess.objects.filter(
                user=request.user, profession=profession
            ).values_list('section_id', flat=True)
        )
        today_access = DailySectionAccess.objects.filter(
            user=request.user, profession=profession, accessed_date=today
        ).select_related('section').first()
        if today_access:
            daily_section_today = today_access.section_id
            daily_limit_section_name = today_access.section.title

        # Section locking logic
        completed_lesson_ids = set(
            lid for lid, prog in lesson_progress.items() if prog.get('completed')
        )
        ordered_sections = list(sections)
        section_completed_map = {}
        for idx, section in enumerate(ordered_sections):
            section_lesson_ids = set(
                section.lessons.exclude(lesson_type='dictionary').values_list('pk', flat=True)
            )
            if not section_lesson_ids:
                section_is_locked[section.id] = False
                section_daily_locked[section.id] = False
                section_completed_map[section.id] = True
                continue

            section_completed = section_lesson_ids.issubset(completed_lesson_ids)
            section_started = bool(section_lesson_ids & completed_lesson_ids)
            section_completed_map[section.id] = section_completed

            if section_completed or section_started:
                section_is_locked[section.id] = False
                section_daily_locked[section.id] = False
            elif idx == 0:
                section_is_locked[section.id] = False
                section_daily_locked[section.id] = False
            else:
                prev_section = ordered_sections[idx - 1]
                prev_ids = set(prev_section.lessons.exclude(lesson_type='dictionary').values_list('pk', flat=True))
                if not prev_ids.issubset(completed_lesson_ids):
                    section_is_locked[section.id] = True
                    section_daily_locked[section.id] = False
                else:
                    section_is_locked[section.id] = False
                    if limit_on and daily_section_today and daily_section_today != section.id:
                        section_daily_locked[section.id] = True
                    else:
                        section_daily_locked[section.id] = False

        # Darslar holati — bölim holatiga qarab (profession_detail bilan bir xil
        # mantiq). Bölim oçiq bölsa ham, tasdiqlanmagan (pending/revision)
        # vazifadan keyingi darslar alohida qulflanadi -- şu joy avval
        # töldirilmagan böş lesson_is_locked qoldirilgani sabab vazifa
        # rad etilgan taqdirda ham keyingi darsga ötib bölardi.
        for section in ordered_sections:
            s_locked = section_is_locked.get(section.id)
            s_daily = section_daily_locked.get(section.id)
            section_lessons = [l for l in section.lessons.all() if l.lesson_type != 'dictionary']
            if s_locked:
                for l in section_lessons:
                    lesson_is_locked[l.pk] = 'sequential'
            elif s_daily:
                for l in section_lessons:
                    lesson_is_locked[l.pk] = 'daily'
            else:
                for l in section_lessons:
                    # Bölim oçilgan bölsa, uning barça dars turlari birga oçiladi.
                    lesson_is_locked[l.pk] = False

        # Classmates
        classmates = CustomUser.objects.filter(
            enrollments__profession=profession, role='student'
        ).exclude(pk=request.user.pk).order_by('-coins')[:10]

        # Course completion
        all_lesson_ids = set(
            Lesson.objects.filter(profession=profession).exclude(lesson_type='dictionary').values_list('pk', flat=True)
        )
        course_completed = all_lesson_ids and all_lesson_ids.issubset(completed_lesson_ids)

        has_empty_section = any(
            not s.lessons.exists() for s in sections
        )

        certificate = Certificate.objects.filter(student=request.user, profession=profession, bundle__isnull=True).first()

    # Yakuniy testlar — alohida UI uçun
    final_test_lessons = []
    has_final_test = False
    if is_enrolled:
        _final_prerequisites_ready = final_test_prerequisites_met(
            request.user, profession, completed_lesson_ids,
        )
        final_test_qs = list(Lesson.objects.filter(
            profession=profession,
            lesson_type='test',
        ).filter(final_lesson_q()).select_related('test', 'quiz__settings'))
        has_final_test = bool(final_test_qs)
        for _fl in final_test_qs:
            _payment_status = None
            _legacy_test = getattr(_fl, 'test', None)
            _pay = FinalTestPayment.objects.filter(
                student=request.user, test=_legacy_test
            ).order_by('-created_at').first() if _legacy_test else None
            if _pay:
                _payment_status = _pay.status
            final_test_lessons.append({
                'lesson': _fl,
                'test': _final_test_info(_fl),
                'payment_status': _payment_status,
                'progress': lesson_progress.get(_fl.pk),
                'token': token_map.get(_fl.pk),
                'locked': not _final_prerequisites_ready,
            })

    return render(request, 'accounts/profession_detail.html', {
        'profession': profession,
        'is_enrolled': is_enrolled,
        'is_teaching': False,
        'enrollment_request': enrollment_request,
        'unenroll_request': unenroll_request,
        'sections': sections,
        'lessons_without_section': lessons_without_section,
        'lesson_progress': lesson_progress,
        'course_roadmap': _build_course_roadmap(
            sections,
            locals().get('section_completed_map') if isinstance(locals().get('section_completed_map'), dict) else {},
            section_is_locked, section_daily_locked,
        ),
        'section_is_locked': section_is_locked,
        'section_daily_locked': section_daily_locked,
        'daily_section_today': daily_section_today if is_enrolled else None,
        'daily_limit_section_name': daily_limit_section_name if is_enrolled else None,
        'classmates': classmates,
        'course_teachers': course_teachers,
        'course_completed': course_completed,
        'has_empty_section': has_empty_section,
        'certificate': certificate,
        'lesson_is_locked': lesson_is_locked,
        'token_map': token_map,
        'course_token': token_obj,
        'final_test_lessons': final_test_lessons,
        'has_final_test': has_final_test,
        'course_bundle_cards': _course_bundle_cards(request.user, profession),
    })


def _course_bundle_cards(user, profession):
    from .course_bundles import bundles_for_course
    try:
        return bundles_for_course(user, profession)
    except Exception:
        import logging
        logging.getLogger(__name__).exception('Birlashgan kurs progressi hisoblanmadi (profession=%s)', profession.pk)
        return []


@login_required
def student_lesson_view(request, token):
    token_obj = _get_token_or_404(request.user, token)
    lesson = token_obj.lesson
    profession = lesson.profession

    if not CourseEnrollment.objects.filter(user=request.user, profession=profession).exists():
        messages.error(request, "Avval kursga yoziling!")
        return redirect('professions')

    # Qoida ilova API'si bilan bitta joyda (accounts/lesson_access.py)
    from .lesson_access import LessonGate
    gate = LessonGate(request.user, profession)
    blocked = gate.block(lesson)
    if blocked:
        messages.warning(request, blocked[1])
        ct = StudentCourseToken.get_or_create(request.user, profession)
        return redirect('student_course_view', token=ct.token)
    gate.record_visit(lesson)

    # Kurs tokenini oliş (orqaga qaytiş uçun)
    course_token = StudentCourseToken.get_or_create(request.user, profession)

    context = {
        'lesson': lesson,
        'profession': profession,
        'student_token': token_obj,
        'course_token': course_token,
        'video_timer_debug': settings.DEBUG,
    }

    # Keyingi darsni topiş
    next_lesson = None
    if lesson.section:
        next_lesson = Lesson.objects.filter(
            profession=profession, section=lesson.section, order__gt=lesson.order
        ).first()
        if not next_lesson:
            next_section = Section.objects.filter(
                profession=profession, is_active=True, order__gt=lesson.section.order
            ).first()
            if next_section:
                next_lesson = Lesson.objects.filter(profession=profession, section=next_section).first()
    else:
        next_lesson = Lesson.objects.filter(
            profession=profession, section__isnull=True, order__gt=lesson.order
        ).first()
    context['next_lesson'] = next_lesson
    if next_lesson:
        context['next_lesson_token'] = StudentLessonToken.get_or_create_for_lesson(request.user, next_lesson)

    if lesson.lesson_type == 'video':
        video = lesson.video
        progress, _ = VideoProgress.objects.get_or_create(user=request.user, video=video)
        # Sahifa render QILINIŞIDANOQ talab vaqti server tomonda yakuniy
        # holatda bölişi kerak: aks holda birinçi heartbeat'da
        # required_seconds qayta hisoblanib, ekrandagi countdown sakrab ketadi.
        _ensure_gating_duration(video)
        _resolve_required_seconds(progress, video)
        start_watch_session(progress)
        if not lesson.ai_article:
            import threading
            _lesson_id = lesson.pk
            _title = lesson.title
            _prof_name = profession.name
            def _bg_generate():
                try:
                    from accounts.models import Lesson as _Lesson
                    art = _generate_ai_article(_title, _prof_name)
                    if art:
                        _Lesson.objects.filter(pk=_lesson_id, ai_article__isnull=True).update(ai_article=art)
                except Exception:
                    pass
            threading.Thread(target=_bg_generate, daemon=True).start()
        context['video'] = video
        context['progress'] = progress
        context['resume_at'] = progress.last_position if not progress.watched else 0
        context['video_coin_amount'] = _video_watch_coin_amount(video, request.user)
        return render(request, 'accounts/lessons/video_lesson.html', context)

    elif lesson.lesson_type == 'homework':
        homework = lesson.homework
        submission = HomeworkSubmission.objects.filter(homework=homework, student=request.user).first()
        context['homework'] = homework
        context['submission'] = submission
        # Sidebar'dagi "Keyingi dars" kartasi şu bayroqqa qarab
        # körsatiladi/berkitiladi -- vazifa hali tasdiqlanmagan bölsa
        # (pending/reviewing/revision) keyingi darsga ötiş tugmasi
        # körinmaydi, chunki server tomonida ham xuddi şu qoida
        # (student_lesson_view'dagi homework_locked_ids tekşiruvi) bilan
        # bloklanadi -- ikkalasi bir xil holatni aks ettirişi kerak.
        # Vazifa TOPŞIRILGAÇ keyingi darsga (test) ötiş kartasi oçiladi — tasdiqni kutmaydi
        context['homework_approved'] = bool(
            not next_lesson
            or next_lesson.section_id == lesson.section_id
            or submission
        )
        context['form'] = HomeworkSubmissionForm()
        return render(request, 'accounts/lessons/homework_lesson.html', context)

    elif lesson.lesson_type == 'test':
        quiz = getattr(lesson, 'quiz', None)
        test = getattr(lesson, 'test', None)
        # Yangi quiz tizimi (agar mavjud bölsa ustun turadi). Qoralama quiz
        # öquvçini töxtatib qöymasin — u holda eski test oçiladi.
        if quiz and quiz.status == Quiz.Status.PUBLISHED:
            return redirect('quizui:quiz_take', pk=quiz.pk)
        # Eski test tizimi
        if test:
            result = TestResult.objects.filter(test=test, student=request.user).order_by('-completed_at').first()
            context['test'] = test
            context['result'] = result
            if test.test_type == 'final':
                pending_payment = FinalTestPayment.objects.filter(
                    student=request.user, test=test, status='pending'
                ).exists() if result else False
                approved_payment = FinalTestPayment.objects.filter(
                    student=request.user, test=test, status='approved'
                ).exists() if result else False
                context['pending_payment'] = pending_payment
                context['approved_payment'] = approved_payment
                return render(request, 'accounts/lessons/final_test_lesson.html', context)
            return render(request, 'accounts/lessons/test_lesson.html', context)
        messages.error(request, "Bu test hali tayyor emas.")
        return redirect('profession_detail', pk=profession.pk)

    elif lesson.lesson_type == 'dictionary':
        dictionary = lesson.dictionary
        words = dictionary.words.all()
        search = request.GET.get('q', '').strip()
        if search:
            words = words.filter(
                Q(word__icontains=search) | Q(translation__icontains=search)
            )
        context['dictionary'] = dictionary
        context['words'] = words
        context['search'] = search
        return render(request, 'accounts/lessons/dictionary_lesson.html', context)

    elif lesson.lesson_type == 'speaking':
        speaking_task = lesson.speaking_task
        submission = SpeakingSubmission.objects.filter(speaking_task=speaking_task, student=request.user).first()
        context['speaking_task'] = speaking_task
        context['submission'] = submission
        return render(request, 'accounts/lessons/speaking_lesson.html', context)

    return redirect('profession_detail', pk=profession.pk)


@login_required
def student_mark_video_watched(request, token):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST only'})

    token_obj = _get_token_or_404(request.user, token)
    lesson = token_obj.lesson

    if lesson.lesson_type != 'video':
        return JsonResponse({'success': False, 'error': 'Not a video lesson'})

    video = lesson.video
    _ensure_gating_duration(video)
    return _complete_video_watch(request, video)


@login_required
def student_submit_homework(request, token):
    token_obj = _get_token_or_404(request.user, token)
    lesson = token_obj.lesson
    homework = get_object_or_404(Homework, lesson=lesson)

    if request.method == 'POST':
        existing = HomeworkSubmission.objects.filter(homework=homework, student=request.user).first()
        if existing:
            messages.info(request, "Siz bu vazifani allaqaçon topşirgansiz!")
            return redirect('student_lesson_view', token=token_obj.token)

        submission_type = request.POST.get('submission_type', 'file')
        submission = None

        if homework.file_only:
            submission_type = 'file'

        if homework.handwritten_only:
            submission_type = 'file'
            uploaded_file = request.FILES.get('file')
            if uploaded_file:
                import os
                ext = os.path.splitext(uploaded_file.name)[1].lower()
                if ext not in ('.jpg', '.jpeg', '.png'):
                    messages.error(request, "Faqat rasm formatida (.jpg, .jpeg, .png) qabul qilinadi!")
                    return redirect('student_lesson_view', token=token_obj.token)

        if submission_type == 'file':
            form = HomeworkSubmissionForm(request.POST, request.FILES)
            if form.is_valid():
                submission = form.save(commit=False)
                submission.homework = homework
                submission.student = request.user
                submission.submission_type = 'file'
                submission.save()
                messages.success(request, "Vazifa muvaffaqiyatli yuborildi!")
                _notify_homework_submitted(request.user, homework)
        else:
            text_content = request.POST.get('text_content', '').strip()
            if text_content:
                submission = HomeworkSubmission.objects.create(
                    homework=homework, student=request.user,
                    submission_type='text', text_content=text_content
                )
                messages.success(request, "Vazifa muvaffaqiyatli yuborildi!")
                _notify_homework_submitted(request.user, homework)
            else:
                messages.error(request, "Matn kiritişingiz kerak!")

        if submission and homework.ai_check and homework.lesson.profession.ai_check_enabled:
            _schedule_ai_check(submission.pk)

    return redirect('student_lesson_view', token=token_obj.token)


@login_required
def student_edit_homework(request, token):
    token_obj = _get_token_or_404(request.user, token)
    lesson = token_obj.lesson
    submission = get_object_or_404(HomeworkSubmission, homework__lesson=lesson, student=request.user)

    if not submission.can_edit:
        messages.error(request, "Tahrirlaş muddati tugagan (1 soat)!")
        return redirect('student_lesson_view', token=token_obj.token)

    if request.method == 'POST':
        submission_type = request.POST.get('submission_type', submission.submission_type)

        if submission_type == 'file':
            new_file = request.FILES.get('file')
            if new_file:
                submission.file = new_file
                submission.submission_type = 'file'
                submission.save()
                messages.success(request, "Topşiriq muvaffaqiyatli tahrirlandi!")
            else:
                messages.error(request, "Fayl tanlang!")
        else:
            text_content = request.POST.get('text_content', '').strip()
            if text_content:
                submission.text_content = text_content
                submission.submission_type = 'text'
                submission.save()
                messages.success(request, "Topşiriq muvaffaqiyatli tahrirlandi!")
            else:
                messages.error(request, "Matn kiriting!")

    return redirect('student_lesson_view', token=token_obj.token)


@login_required
def student_submit_speaking(request, token):
    token_obj = _get_token_or_404(request.user, token)
    lesson = token_obj.lesson
    speaking_task = get_object_or_404(SpeakingTask, lesson=lesson)

    if request.method != 'POST':
        return redirect('student_lesson_view', token=token_obj.token)

    existing = SpeakingSubmission.objects.filter(speaking_task=speaking_task, student=request.user).first()
    if existing:
        messages.info(request, "Siz bu vazifani allaqaçon topşirgansiz!")
        return redirect('student_lesson_view', token=token_obj.token)

    audio_file = request.FILES.get('audio_file')
    if not audio_file:
        messages.error(request, "Audio faylni yuklang!")
        return redirect('student_lesson_view', token=token_obj.token)

    submission = SpeakingSubmission.objects.create(
        speaking_task=speaking_task, student=request.user, audio_file=audio_file,
    )

    if speaking_task.ai_check and speaking_task.lesson.profession.ai_check_enabled:
        # Audio transkripsiya + baholaş 20–60 soniya oladi — fonda bajariladi.
        from teacher.views import apply_ai_speaking_grade
        ai_jobs.submit(apply_ai_speaking_grade, submission.pk)
        messages.success(request, "Speaking vazifa topşirildi! 🤖 AI uni bir neça daqiqada tekşiradi — natijani şu sahifada körasiz.")
    else:
        messages.success(request, "Speaking vazifa muvaffaqiyatli topşirildi!")

    return redirect('student_lesson_view', token=token_obj.token)


@login_required
def student_start_test(request, token):
    token_obj = _get_token_or_404(request.user, token)
    lesson = token_obj.lesson
    test = get_object_or_404(Test, lesson=lesson)
    profession = lesson.profession

    if not CourseEnrollment.objects.filter(user=request.user, profession=profession).exists():
        messages.error(request, "Avval kursga yoziling!")
        return redirect('profession_detail', pk=profession.pk)

    if (test.test_type == 'final'
            and not final_test_prerequisites_met(request.user, profession)):
        messages.warning(
            request,
            "Yakuniy test oçilişi uçun avval kursdagi barça video, vazifa va testlarni bajaring!",
        )
        course_token = StudentCourseToken.get_or_create(request.user, profession)
        return redirect('student_course_view', token=course_token.token)

    existing_result = TestResult.objects.filter(test=test, student=request.user).first()
    if existing_result:
        if test.test_type == 'final':
            approved_payment = FinalTestPayment.objects.filter(
                student=request.user, test=test, status='approved'
            ).first()
            if not approved_payment:
                messages.info(request, "Yakuniy testni qayta işlaş uçun tölov qilişingiz kerak (65,000 söm).")
                return redirect('final_test_payment', pk=test.pk)
            approved_payment.delete()
        elif not test.allow_retry:
            messages.info(request, "Siz bu testni allaqaçon topşirgansiz.")
            return redirect('student_test_result', token=token_obj.token, result_pk=existing_result.pk)

    sq_key = f'test_{test.pk}_questions'
    st_key = f'test_{test.pk}_start'
    sa_key = f'test_{test.pk}_answer_order'
    token_key = f'test_{test.pk}_submit_token'

    active_question_ids = request.session.get(sq_key)
    submit_token = request.session.get(token_key)

    if active_question_ids and not submit_token:
        questions_qs = TestQuestion.objects.filter(pk__in=active_question_ids).prefetch_related('answers')
        question_map = {q.pk: q for q in questions_qs}
        questions = [question_map[qid] for qid in active_question_ids if qid in question_map]

        answer_order = request.session.get(sa_key, {})
        for q in questions:
            ordered_ids = answer_order.get(str(q.pk))
            if ordered_ids:
                answer_map = {a.pk: a for a in q.answers.all()}
                ordered_answers = [answer_map[aid] for aid in ordered_ids if aid in answer_map]
                q._prefetched_objects_cache['answers'] = ordered_answers

        start_time_str = request.session.get(st_key)
        remaining = test.time_limit * 60
        if start_time_str:
            from datetime import datetime as dt
            try:
                started = dt.fromisoformat(start_time_str)
                elapsed = (timezone.now() - started).total_seconds()
                remaining = max(0, test.time_limit * 60 - int(elapsed))
            except (ValueError, TypeError):
                pass

        if remaining <= 0:
            request.session.pop(sq_key, None)
            request.session.pop(st_key, None)
            request.session.pop(sa_key, None)
            return redirect('student_lesson_view', token=token_obj.token)

        template = 'accounts/lessons/final_test_take.html' if test.test_type == 'final' else 'accounts/lessons/test_take.html'
        return render(request, template, {
            'test': test,
            'questions': questions,
            'remaining_time': remaining,
            'student_token': token_obj,
        })

    if existing_result and test.allow_retry and test.test_type != 'final':
        retry_cost = 5 if request.user.is_premium else 10
        request.user.remove_coins(retry_cost, f"Test qayta topşiriş: {test.lesson.title}")

    all_questions = list(test.questions.all().distinct().prefetch_related('answers'))

    import random

    if test.question_count and test.question_count > 0 and len(all_questions) > test.question_count:
        questions = random.sample(all_questions, test.question_count)
    else:
        questions = all_questions[:]

    if test.shuffle_questions:
        random.shuffle(questions)

    if test.shuffle_answers:
        for q in questions:
            answers_list = list(q.answers.all())
            random.shuffle(answers_list)
            q._prefetched_objects_cache['answers'] = answers_list

    request.session[sq_key] = [q.pk for q in questions]
    request.session[st_key] = timezone.now().isoformat()
    request.session.pop(token_key, None)

    answer_order = {}
    for q in questions:
        cached = getattr(q, '_prefetched_objects_cache', {}).get('answers')
        if cached is not None:
            answer_order[str(q.pk)] = [a.pk for a in cached]
        else:
            answer_order[str(q.pk)] = [a.pk for a in q.answers.all()]
    request.session[sa_key] = answer_order

    remaining = test.time_limit * 60

    template = 'accounts/lessons/final_test_take.html' if test.test_type == 'final' else 'accounts/lessons/test_take.html'
    return render(request, template, {
        'test': test,
        'questions': questions,
        'remaining_time': remaining,
        'student_token': token_obj,
    })


def grade_legacy_test(user, test, questions, answer_for, started_at):
    """Eski test javoblarini baholaydi, coin va sertifikatni beradi. Sayt va ilova bitta yöldan ötadi.

    `answer_for(question)` tanlangan javob ID'sini qaytaradi. Javob aynan şu savolga
    tegişli bölişi şart: boşqa savolning töğri javobi bilan ball olib bölmaydi.
    Qaytaradi: (natija, coin berildimi, sertifikat berildimi).
    """
    questions = list(questions)
    total = len(questions)
    correct = 0
    result = TestResult.objects.create(
        test=test, student=user,
        score=0, total_questions=total, correct_answers=0,
        started_at=started_at, passed=False
    )
    for question in questions:
        selected_answer = None
        answer_id = answer_for(question)
        if str(answer_id or '').isdigit():
            selected_answer = TestAnswer.objects.filter(pk=int(answer_id), question=question).first()
        is_correct = bool(selected_answer and selected_answer.is_correct)
        if is_correct:
            correct += 1
        TestUserAnswer.objects.create(
            result=result, question=question,
            selected_answer=selected_answer, is_correct=is_correct
        )

    result.score = int((correct / total) * 100) if total > 0 else 0
    result.correct_answers = correct
    result.passed = result.score >= test.passing_score
    result.save()

    is_first_attempt = not TestResult.objects.filter(test=test, student=user).exclude(pk=result.pk).exists()
    from .coin_rules import test_coins, test_coin_reason
    first_coins = 0
    certificate_issued = False
    title = test.lesson.title
    if is_first_attempt:
        # Faqat birinçi urinişda: har bir töğri javob 5 coin + ötilsa 100 coin
        first_coins = test_coins(correct, result.passed)
        if first_coins:
            user.add_coins(first_coins, test_coin_reason(title, correct, result.passed))
            result.coin_awarded = True
            result.save()
    if result.passed:
        ActivityLog.objects.create(
            user=user, action_type='submit_test',
            description=f"Test topşirdi: {title} - {correct}/{total} töğri" + (f", +{first_coins} coin" if first_coins else "")
        )
        # 🎓 Yakuniy testdan ötdi — sertifikatni avtomatik beriş
        if test.test_type == 'final' and user.is_learner:
            try:
                from .certificates import issue_course_certificate
                cert, _cert_err = issue_course_certificate(user, test.lesson.profession)
                certificate_issued = bool(cert and cert.file)
            except Exception:
                pass
    else:
        ActivityLog.objects.create(
            user=user, action_type='submit_test',
            description=f"Test topşirdi (muvaffaqiyatsiz): {title} - {correct}/{total} töğri" + (f", +{first_coins} coin" if first_coins else "")
        )
    return result, first_coins, certificate_issued


def notify_legacy_test(user, result):
    """Test natijasi xabarlari. Baho yozilib bölgandan keyin, tranzaksiyadan taşqarida çaqiriladi."""
    from .notifications import on_test_completed
    on_test_completed(user, result)
    lesson = result.test.lesson
    passed_text = "✅ Muvaffaqiyatli" if result.passed else "❌ Muvaffaqiyatsiz"
    notify_user(
        user.telegram_id,
        f"📊 <b>Test natijasi!</b>\n\n"
        f"📚 Kurs: {lesson.profession.name}\n"
        f"📖 Dars: {lesson.title}\n"
        f"✏️ Natija: {result.correct_answers}/{result.total_questions} töğri ({result.score}%)\n"
        f"{passed_text}"
    )


@login_required
def student_submit_test(request, token):
    token_obj = _get_token_or_404(request.user, token)
    lesson = token_obj.lesson
    test = get_object_or_404(Test, lesson=lesson)

    if (test.test_type == 'final'
            and not final_test_prerequisites_met(request.user, lesson.profession)):
        messages.warning(
            request,
            "Yakuniy test oçilişi uçun avval kursdagi barça video, vazifa va testlarni bajaring!",
        )
        course_token = StudentCourseToken.get_or_create(request.user, lesson.profession)
        return redirect('student_course_view', token=course_token.token)

    if request.method != 'POST':
        return redirect('student_start_test', token=token_obj.token)

    submit_token = request.session.get(f'test_{test.pk}_submit_token')
    if submit_token:
        last_result = TestResult.objects.filter(test=test, student=request.user).order_by('-id').first()
        if last_result:
            return redirect('student_test_result', token=token_obj.token, result_pk=last_result.pk)
    request.session[f'test_{test.pk}_submit_token'] = True

    request.session.pop(f'test_{test.pk}_answer_order', None)

    start_time = request.session.get(f'test_{test.pk}_start')
    started_at = timezone.now()
    if start_time:
        from datetime import datetime
        started_at = datetime.fromisoformat(start_time)

        elapsed_seconds = (timezone.now() - started_at).total_seconds()
        allowed_seconds = test.time_limit * 60 + 60
        if elapsed_seconds > allowed_seconds:
            messages.warning(request, "Vaqt çegarasi ötgan! Natijalar server vaqti bilan tekşirildi.")

    shown_question_ids = request.session.get(f'test_{test.pk}_questions')
    if shown_question_ids:
        questions = test.questions.filter(pk__in=shown_question_ids)
    else:
        questions = test.questions.all()

    result, first_coins, certificate_issued = grade_legacy_test(
        request.user, test, questions,
        lambda question: request.POST.get(f'question_{question.pk}'),
        started_at,
    )
    correct, total, score = result.correct_answers, result.total_questions, result.score

    if result.passed:
        if first_coins:
            messages.success(request, f"Tabriklaymiz! Test muvaffaqiyatli! +{first_coins} coin oldingiz ({correct} ta töğri javob + ötganlik bonusi)!")
        else:
            messages.success(request, f"Tabriklaymiz! Test muvaffaqiyatli!")
        if certificate_issued:
            messages.success(
                request,
                "🎓 Tabriklaymiz! Sizga kurs sertifikati berildi — "
                "kurs sahifasidan yuklab olişingiz mumkin!"
            )
    else:
        messages.warning(request, f"Testdan öta olmadingiz. {correct}/{total} töğri javob.")
        if first_coins:
            messages.info(request, f"+{first_coins} coin oldingiz ({correct} ta töğri javob).")

    notify_legacy_test(request.user, result)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        from django.urls import reverse
        redirect_url = reverse('student_test_result', kwargs={'token': token_obj.token, 'result_pk': result.pk})
        return JsonResponse({
            'ok': True,
            'result_pk': result.pk,
            'redirect': redirect_url,
            'score': score,
            'passed': result.passed,
            'correct': correct,
            'total': total,
        })

    return redirect('student_test_result', token=token_obj.token, result_pk=result.pk)


@login_required
def student_test_result(request, token, result_pk):
    token_obj = _get_token_or_404(request.user, token)
    lesson = token_obj.lesson
    result = get_object_or_404(TestResult, pk=result_pk, student=request.user, test__lesson=lesson)
    user_answers = result.user_answers.all().select_related('question', 'selected_answer')

    profession = lesson.profession
    section = lesson.section

    video_lesson = None
    homework_lesson = None
    same_section_lessons = Lesson.objects.filter(profession=profession, section=section) if section else Lesson.objects.filter(profession=profession)
    for l in same_section_lessons:
        if l.lesson_type == 'video' and not video_lesson:
            video_lesson = l
        elif l.lesson_type == 'homework' and not homework_lesson:
            homework_lesson = l

    video_lesson_token = None
    homework_lesson_token = None
    if video_lesson:
        video_lesson_token = StudentLessonToken.get_or_create_for_lesson(request.user, video_lesson)
    if homework_lesson:
        homework_lesson_token = StudentLessonToken.get_or_create_for_lesson(request.user, homework_lesson)

    course_token = StudentCourseToken.get_or_create(request.user, profession)

    return render(request, 'accounts/lessons/test_result.html', {
        'result': result,
        'user_answers': user_answers,
        'video_lesson': video_lesson,
        'homework_lesson': homework_lesson,
        'student_token': token_obj,
        'video_lesson_token': video_lesson_token,
        'homework_lesson_token': homework_lesson_token,
        'course_token': course_token,
    })


@login_required
def complete_profile(request):
    """Profilni töldiriş / akkauntni tasdiqlaş (verify).

    Foydalanuvçi pasport yoki metrika kiritib profilni tasdiqlaydi. Formani
    ötkazib yuborişi (skip) mumkin — bunda joriy sessiyada davom etadi, ammo
    keyingi login'da yana söraladi (skip belgisi login'da tozalanadi).
    """
    from .forms import StudentProfileCompleteForm

    # Agar profil töliq töldirilgan (tuğilgan sana bilan) bölsa
    if request.user.is_profile_completed and request.user.birth_date:
        return redirect('home')

    # ⏭️ "Keyinroq töldiraman" — şu sessiya uçun ötkazib yuboriladi.
    # Faqat tuğilgan sanasi BOR öquvçilar skip qilişi mumkin: sana
    # yöq bölsa keyingi kirişda yana söraladi.
    if request.method == 'POST' and request.POST.get('action') == 'skip':
        if request.user.birth_date:
            request.session['profile_verify_skipped'] = True
            messages.info(request, "Profil töldiriş keyinga qoldirildi. Keyingi kirişda yana söraladi.")
            return redirect('home')
        messages.warning(request, "Tuğilgan sana majburiy — iltimos formani töldiring!")
        return redirect('complete_profile')

    if request.method == 'POST':
        form = StudentProfileCompleteForm(request.POST, request.FILES, instance=request.user)
        if form.is_valid():
            user = form.save(commit=False)
            if user.check_profile_completed():
                user.is_profile_completed = True
                user.is_verified = True  # hujjat töldirildi — akkaunt tasdiqlandi
            user.save()
            if user.is_profile_completed:
                request.session.pop('profile_verify_skipped', None)
                messages.success(request, "Profilingiz töldirildi va akkauntingiz tasdiqlandi! ✅")
                return redirect('home')
            else:
                messages.warning(request, "Iltimos, barça majburiy maydonlarni töldiring!")
    else:
        form = StudentProfileCompleteForm(instance=request.user)

    return render(request, 'accounts/complete_profile.html', {
        'form': form,
        'completion_percent': request.user.profile_completion_percent,
    })


# Profile views
@login_required
def profile_view(request):
    profile_fields = ('first_name', 'last_name', 'phone', 'email', 'photo', 'birth_date', 'address', 'bio')
    profile_filled = sum(bool(getattr(request.user, field, None)) for field in profile_fields)
    profile_summary = {
        'completion': round(profile_filled / len(profile_fields) * 100),
        'filled': profile_filled,
        'total': len(profile_fields),
    }
    if request.user.is_teacher:
        teaching_courses = request.user.teaching_professions.all()
        profile_summary['courses'] = teaching_courses.count()
        profile_summary['students'] = CourseEnrollment.objects.filter(
            profession__in=teaching_courses, user__role='student'
        ).values('user_id').distinct().count()
        profile_summary['pending'] = HomeworkSubmission.objects.filter(
            homework__lesson__profession__in=teaching_courses, status='pending'
        ).count()
    certificates = Certificate.objects.filter(student=request.user).select_related('profession')
    enrollments = request.user.enrollments.all().select_related('profession')
    
    # Tugallangan kurslar (sertifikat berilgan)
    # Birlashgan kurs sertifikati birinçi kursni "tugatilgan" qilib körsatmasin.
    completed_profession_ids = certificates.filter(bundle__isnull=True).values_list('profession_id', flat=True)
    completed_courses = []
    active_courses = []
    
    for enrollment in enrollments:
        if enrollment.profession_id in completed_profession_ids:
            cert = certificates.filter(profession_id=enrollment.profession_id, bundle__isnull=True).first()
            completed_courses.append({
                'enrollment': enrollment,
                'certificate': cert,
            })
        else:
            active_courses.append(enrollment)
    
    # Student balansi
    student_balance = None
    if request.user.role == 'student':
        from .models import StudentBalance
        student_balance, _ = StudentBalance.objects.get_or_create(user=request.user)

    # Coin statistika
    from coin.models import CoinTransaction
    total_earned_coins = CoinTransaction.objects.filter(user=request.user, action='add').aggregate(total=Sum('amount'))['total'] or 0
    total_spent_coins = CoinTransaction.objects.filter(user=request.user, action__in=['remove', 'purchase']).aggregate(total=Sum('amount'))['total'] or 0
    
    # Ball statistika
    test_results = TestResult.first_attempts().filter(student=request.user)
    total_tests_taken = test_results.count()
    avg_test_score = round(test_results.aggregate(avg=Avg('score'))['avg'] or 0, 1)
    
    hw_submissions = HomeworkSubmission.objects.filter(student=request.user)
    total_homeworks_submitted = hw_submissions.count()
    avg_homework_grade = round(hw_submissions.filter(grade__isnull=False).aggregate(avg=Avg('grade'))['avg'] or 0, 1)
    
    # XP / Daraja tizimi (boş sahifa bilan bir xil): 100 XP = 1 daraja
    _xp = int(getattr(request.user, 'coins', 0) or 0)
    profile_level = _xp // 100 + 1
    profile_xp = _xp
    profile_xp_in_level = _xp % 100
    profile_xp_to_next = 100 - profile_xp_in_level
    profile_xp_pct = profile_xp_in_level  # 100 XP bir daraja => foiz = darajadagi XP

    return render(request, 'accounts/profile.html', {
        'profile_summary': profile_summary,
        'certificates': certificates,
        'enrollments': enrollments,
        'completed_courses': completed_courses,
        'active_courses': active_courses,
        'total_earned_coins': total_earned_coins,
        'total_spent_coins': total_spent_coins,
        'total_tests_taken': total_tests_taken,
        'avg_test_score': avg_test_score,
        'total_homeworks_submitted': total_homeworks_submitted,
        'avg_homework_grade': avg_homework_grade,
        'student_balance': student_balance,
        'profile_level': profile_level,
        'profile_xp': profile_xp,
        'profile_xp_in_level': profile_xp_in_level,
        'profile_xp_to_next': profile_xp_to_next,
        'profile_xp_pct': profile_xp_pct,
        'social_accounts': {a.provider: a for a in request.user.social_accounts.all()},
        'connected_now': request.GET.get('connected', ''),
    })


@login_required
def site_settings(request):
    """Sayt sozlamalari sahifasi"""
    return render(request, 'accounts/settings.html')


@login_required
@require_POST
def save_weather_effect(request):
    """Ob-havo effekti tanlovini akkauntga saqlaş (JSON, CSRF bilan). Faqat premium."""
    from .nav_preferences import can_customize
    if not can_customize(request.user):
        return JsonResponse({'status': 'error', 'error': 'premium_required'}, status=403)
    try:
        data = json.loads(request.body or b'{}')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'status': 'error'}, status=400)

    effect = data.get('effect') if isinstance(data, dict) else None
    if effect not in ('off', 'auto', 'winter', 'spring', 'summer', 'autumn'):
        return JsonResponse({'status': 'error'}, status=400)

    request.user.weather_effect = effect
    request.user.save(update_fields=['weather_effect'])
    return JsonResponse({'status': 'ok'})


@login_required
@require_POST
def save_navigation_preferences(request):
    """Navigatsiya körinişi sozlamalarini akkauntga saqlaş (JSON, CSRF bilan)."""
    from .nav_preferences import PREMIUM_ONLY, can_customize, clean, effective, get_prefs, save_prefs
    try:
        data = json.loads(request.body or b'{}')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'status': 'error', 'error': "Notöğri sörov"}, status=400)
    changes = clean(data)
    if not changes:
        return JsonResponse({'status': 'error', 'error': "Sozlama topilmadi"}, status=400)
    if not can_customize(request.user):
        # "Saqlaş" tugmasi barça sozlamani yuboradi: standart qiymat özgariş emas, shuning uçun rad etilmaydi
        if any(key in changes and changes[key] != value for key, value in PREMIUM_ONLY.items()):
            return JsonResponse({'status': 'error', 'error': 'premium_required'}, status=403)
        changes = {key: value for key, value in changes.items() if key not in PREMIUM_ONLY}
        if not changes:
            return JsonResponse({'status': 'ok', 'prefs': get_prefs(request)})
    prefs = effective(request.user, save_prefs(request.user, changes))
    return JsonResponse({'status': 'ok', 'prefs': prefs})


@login_required
def classmates_view(request, pk):
    """Kursdoşlar röyxati"""
    profession = get_object_or_404(Profession, pk=pk)
    
    # Öqituvçi faqat özi tayinlangan kurslarga kirişi mumkin
    if request.user.is_teacher and not request.user.teaching_professions.filter(pk=profession.pk).exists():
        messages.error(request, "Sizda bu kursga kiriş huquqi yöq!")
        return redirect('professions')
    
    # Faqat premium foydalanuvçilar, admin va öqituvçilar kirişi mumkin
    if not request.user.is_admin and not request.user.is_teacher:
        if not request.user.is_premium:
            messages.warning(request, "Kursdoşlar bölimi faqat Premium foydalanuvçilar uçun! 💎")
            return redirect('profession_detail', pk=pk)
        if not CourseEnrollment.objects.filter(user=request.user, profession=profession).exists():
            messages.error(request, "Avval kursga yoziling!")
            return redirect('profession_detail', pk=pk)
    
    # Kursdoşlar
    classmates = CustomUser.objects.filter(
        enrollments__profession=profession,
        role='student'
    ).exclude(pk=request.user.pk).order_by('-coins', 'first_name')

    from .premium_bonus import is_active_premium

    classmates = list(classmates)
    for student in classmates:
        student.premium_active = is_active_premium(student)

    return render(request, 'accounts/classmates.html', {
        'profession': profession,
        'classmates': classmates,
        'online_count': sum(1 for s in classmates if s.is_online),
        'premium_count': sum(1 for s in classmates if s.premium_active),
    })


@login_required
def student_public_profile(request, pk):
    """Boşqa öquvçining umumiy profili"""
    student = get_object_or_404(CustomUser, pk=pk)
    
    # Faqat öquvçilar körinişi mumkin
    if student.role != 'student':
        messages.error(request, "Bu sahifa mavjud emas!")
        return redirect('home')
    
    # Bloklangan foydalanuvçi tekşiruvi
    if hasattr(request.user, 'is_blocked') and request.user.is_blocked:
        messages.error(request, "Sizning akkauntingiz bloklangan!")
        return redirect('home')
    
    # Bloklangan öquvçi profili körinmas
    if student.is_blocked:
        messages.error(request, "Bu profil mavjud emas!")
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

    # Barça kurslar (yozilgan)
    all_enrollments = student_enrollments
    
    # Ball statistika
    test_results_qs = TestResult.first_attempts().filter(student=student)
    total_tests_taken = test_results_qs.count()
    avg_test_score = round(test_results_qs.aggregate(avg=Avg('score'))['avg'] or 0, 1)
    
    hw_submissions = HomeworkSubmission.objects.filter(student=student)
    total_homeworks_submitted = hw_submissions.count()
    avg_homework_grade = round(hw_submissions.filter(grade__isnull=False).aggregate(avg=Avg('grade'))['avg'] or 0, 1)
    
    return render(request, 'accounts/student_public_profile.html', {
        'student': student,
        'common_courses': common_courses,
        'certificates': certificates,
        'completed_count': completed_count,
        'all_enrollments': all_enrollments,
        'total_tests_taken': total_tests_taken,
        'avg_test_score': avg_test_score,
        'total_homeworks_submitted': total_homeworks_submitted,
        'avg_homework_grade': avg_homework_grade,
    })


@login_required
def teacher_public_profile(request, pk):
    """Öqituvçining umumiy profili (öquvçilar uçun)"""
    from teacher.models import TeacherCertificate
    teacher = get_object_or_404(CustomUser, pk=pk, role='teacher')
    courses = teacher.teaching_professions.all()
    certificates = TeacherCertificate.objects.filter(teacher=teacher, is_visible=True)

    return render(request, 'accounts/teacher_public_profile.html', {
        'teacher': teacher,
        'courses': courses,
        'certificates': certificates,
    })


@login_required
def profile_edit_view(request):
    original_email = request.user.email
    if request.method == 'POST':
        form = UserProfileForm(request.POST, request.FILES, instance=request.user)
        if form.is_valid():
            user = form.save(commit=False)
            if user.email.strip().lower() != original_email.strip().lower():
                from .email_verification import stage_email, send_verification
                stage_email(user, user.email)
                user.email = original_email
                user.save()
                if user.pending_email:
                    if send_verification(user, request):
                        messages.info(request, 'Yangi emailga tasdiqlaş havolasi yuborildi. Hali boğlanmagan.')
                    else:
                        messages.warning(request, 'Tasdiqlaş havolasi yuborilmadi. Qayta yuborişni bosing.')
            else:
                user.save()
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
                messages.error(request, "Joriy parol notöğri!")
            else:
                request.user.set_password(form.cleaned_data['new_password1'])
                request.user.save()
                login(request, request.user, backend=request.session.get('_auth_user_backend', 'django.contrib.auth.backends.ModelBackend'))
                
                # Xavfsizlik xabari
                from .notifications import check_password_changed
                check_password_changed(request.user)
                
                messages.success(request, "Parol muvaffaqiyatli özgartirildi!")
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
    tests_query = TestResult.first_attempts().filter(student=student)
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
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from io import BytesIO
    
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=30, bottomMargin=30)
    elements = []
    styles = getSampleStyleSheet()
    
    heading_style = ParagraphStyle(
        'SectionHeading',
        parent=styles['Heading2'],
        spaceAfter=8,
        spaceBefore=16,
        textColor=colors.HexColor('#6366f1')
    )
    
    def make_table(data, col_widths=None):
        if col_widths is None:
            col_widths = [220, 250]
        t = Table(data, colWidths=col_widths)
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#6366f1')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#dee2e6')),
            ('PADDING', (0, 0), (-1, -1), 8),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8f9fa')]),
        ]))
        return t
    
    elements.append(Paragraph(f"Öquvçi anketa: {student.full_name}", styles['Heading1']))
    elements.append(Paragraph(f"Sana: {timezone.now().strftime('%d.%m.%Y')}", styles['Normal']))
    elements.append(Spacer(1, 16))
    
    # Şaxsiy ma'lumotlar
    elements.append(Paragraph("Şaxsiy ma'lumotlar", heading_style))
    personal_data = [
        ["Ma'lumot", 'Qiymat'],
        ['Ism', student.first_name or '-'],
        ['Familiya', student.last_name or '-'],
        ["Tuğilgan kuni", student.birth_date.strftime('%d.%m.%Y') if student.birth_date else '-'],
        ['Jinsi', student.get_gender_display() if student.gender else '-'],
        ['Millati', student.nationality or '-'],
        ['Telefon', student.phone or '-'],
        ['Manzil', student.address or '-'],
    ]
    elements.append(make_table(personal_data))
    elements.append(Spacer(1, 12))
    
    # Pasport ma'lumotlari
    elements.append(Paragraph("Pasport ma'lumotlari", heading_style))
    passport_data = [
        ["Ma'lumot", 'Qiymat'],
        ['Pasport seriyasi', student.passport_series or '-'],
        ['Pasport raqami', student.passport_number or '-'],
        ['JŞŞIR (PINFL)', student.jshshir or '-'],
        ['Berilgan sana', student.passport_issued_date.strftime('%d.%m.%Y') if student.passport_issued_date else '-'],
    ]
    elements.append(make_table(passport_data))
    elements.append(Spacer(1, 12))
    
    # Ota ma'lumotlari
    elements.append(Paragraph("Ota ma'lumotlari", heading_style))
    father_data = [
        ["Ma'lumot", 'Qiymat'],
        ["Töliq ismi", student.father_name or '-'],
        ['Telefon raqami', student.father_phone or '-'],
        ['Vafot etgan', 'Ha' if student.father_deceased else "Yöq"],
    ]
    elements.append(make_table(father_data))
    elements.append(Spacer(1, 12))
    
    # Ona ma'lumotlari
    elements.append(Paragraph("Ona ma'lumotlari", heading_style))
    mother_data = [
        ["Ma'lumot", 'Qiymat'],
        ["Töliq ismi", student.mother_name or '-'],
        ['Telefon raqami', student.mother_phone or '-'],
        ['Vafot etgan', 'Ha' if student.mother_deceased else "Yöq"],
    ]
    elements.append(make_table(mother_data))
    elements.append(Spacer(1, 12))
    
    # Asosiy aloqa
    elements.append(Paragraph("Asosiy aloqa", heading_style))
    contact_data = [
        ["Ma'lumot", 'Qiymat'],
        ['Ota-ona telefoni', student.parent_phone or '-'],
    ]
    elements.append(make_table(contact_data))
    elements.append(Spacer(1, 12))
    
    # Maktab ma'lumotlari
    elements.append(Paragraph("Maktab ma'lumotlari", heading_style))
    school_data = [
        ["Ma'lumot", 'Qiymat'],
        ['Maktab nomi', student.school_name or '-'],
        ['Maktab raqami', student.school_number or '-'],
    ]
    elements.append(make_table(school_data))
    elements.append(Spacer(1, 16))
    
    # Statistika
    elements.append(Paragraph("Öquv statistikasi", heading_style))
    stats_data = [
        ["Körsatkiç", 'Qiymat'],
        ['Jami coinlar', str(student.coins)],
        ["Körilgan videolar", str(VideoProgress.objects.filter(user=student, watched=True).count())],
        ['Test natijalari', str(TestResult.objects.filter(student=student).count())],
        ['Topşirilgan vazifalar', str(HomeworkSubmission.objects.filter(student=student).count())],
    ]
    elements.append(make_table(stats_data))
    
    doc.build(elements)
    buffer.seek(0)
    
    response = HttpResponse(buffer, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="student_anketa_{student.username}.pdf"'
    return response


@login_required
def export_system_pdf(request):
    if not request.user.is_admin:
        messages.error(request, "Bu sahifaga kiriş huquqingiz yöq!")
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
    
    # ===== FILTRLAR (yil / oy / kurs yönalişi) =====
    f = _parse_stats_filters(request)
    start_date, end_date, profession = f['start_date'], f['end_date'], f['profession']
    today = timezone.now().date()

    def date_range(field):
        kw = {}
        if start_date:
            kw[f'{field}__date__gte'] = start_date
        if end_date:
            kw[f'{field}__date__lt'] = end_date
        return kw

    elements.append(Paragraph("Tizim Statistikasi - Umumiy Hisobot", styles['Title']))
    elements.append(Paragraph(f"Hisobot sanasi: {timezone.now().strftime('%d.%m.%Y %H:%M')}", styles['Normal']))
    # Filtr yorliği
    period_style = ParagraphStyle(
        'PeriodStyle', parent=styles['Normal'],
        fontSize=11, textColor=colors.HexColor('#dc2626'), spaceBefore=4
    )
    elements.append(Paragraph(f"Filtr (davr / yönaliş): {f['label']}", period_style))
    elements.append(Spacer(1, 20))

    # ===== 1. Foydalanuvçilar =====
    users_qs = CustomUser.objects.all()
    students_qs = CustomUser.objects.filter(role='student')
    teachers_qs = CustomUser.objects.filter(role='teacher')
    if profession:
        students_qs = students_qs.filter(enrollments__profession=profession).distinct()
        teachers_qs = teachers_qs.filter(profession=profession).distinct()
        users_qs = CustomUser.objects.filter(
            Q(enrollments__profession=profession) | Q(profession=profession)
        ).distinct()
    if start_date or end_date:
        djf = date_range('date_joined')
        users_qs = users_qs.filter(**djf)
        students_qs = students_qs.filter(**djf)
        teachers_qs = teachers_qs.filter(**djf)

    total_users = users_qs.count()
    total_students = students_qs.count()
    total_teachers = teachers_qs.count()
    total_admins = CustomUser.objects.filter(role='admin').count()
    
    elements.append(Paragraph("1. Foydalanuvçilar statistikasi", heading_style))
    user_data = [
        ["Körsatkiç", 'Soni'],
        ['Jami foydalanuvçilar', str(total_users)],
        ["Öquvçilar", str(total_students)],
        ["Öqituvçilar", str(total_teachers)],
        ['Adminlar', str(total_admins)],
    ]
    elements.append(create_table(user_data))
    elements.append(Spacer(1, 15))
    
    # ===== 2. Kurslar va talabalar =====
    enroll_date_q = Q()
    if start_date:
        enroll_date_q &= Q(enrollments__enrolled_at__date__gte=start_date)
    if end_date:
        enroll_date_q &= Q(enrollments__enrolled_at__date__lt=end_date)
    courses_qs = Profession.objects.all()
    if profession:
        courses_qs = courses_qs.filter(pk=profession.pk)
    total_courses = courses_qs.count()
    courses_with_students = courses_qs.annotate(
        student_count=Count('enrollments', filter=enroll_date_q)
    ).order_by('-student_count')
    
    elements.append(Paragraph("2. Kurslar va talabalar", heading_style))
    course_data = [['Kurs nomi', "Öquvçilar soni"]]
    for course in courses_with_students[:10]:
        course_data.append([course.name[:40], str(course.student_count)])
    if total_courses > 10:
        course_data.append(['...va yana', f'{total_courses - 10} ta kurs'])
    elements.append(create_table(course_data))
    elements.append(Spacer(1, 15))
    
    # ===== 3. Dars materiallari =====
    lessons_qs = Lesson.objects.all()
    videos_qs = VideoLesson.objects.all()
    tests_qs = Test.objects.all()
    hw_created_qs = Homework.objects.all()
    if profession:
        lessons_qs = lessons_qs.filter(profession=profession)
        videos_qs = videos_qs.filter(lesson__profession=profession)
        tests_qs = tests_qs.filter(lesson__profession=profession)
        hw_created_qs = hw_created_qs.filter(lesson__profession=profession)
    total_lessons = lessons_qs.count()
    total_videos = videos_qs.count()
    total_tests = tests_qs.count()
    total_homeworks_created = hw_created_qs.count()
    
    elements.append(Paragraph("3. Dars materiallari", heading_style))
    lesson_data = [
        ["Körsatkiç", 'Soni'],
        ['Jami darslar', str(total_lessons)],
        ['Videodarslar', str(total_videos)],
        ['Testlar', str(total_tests)],
        ['Uy vazifalari', str(total_homeworks_created)],
    ]
    elements.append(create_table(lesson_data))
    elements.append(Spacer(1, 15))
    
    # ===== 4. Test natijalari =====
    tr_qs = TestResult.objects.all()
    if profession:
        tr_qs = tr_qs.filter(test__lesson__profession=profession)
    tr_qs = tr_qs.filter(**date_range('completed_at'))
    total_test_results = tr_qs.count()
    passed_tests = tr_qs.filter(passed=True).count()
    failed_tests = total_test_results - passed_tests
    avg_score = tr_qs.aggregate(avg=Avg('score'))['avg'] or 0
    
    elements.append(Paragraph("4. Test natijalari", heading_style))
    test_data = [
        ["Körsatkiç", 'Qiymat'],
        ["Jami topşirilgan testlar", str(total_test_results)],
        ["Muvaffaqiyatli ötganlar", str(passed_tests)],
        ["Muvaffaqiyatsiz", str(failed_tests)],
        ["Örtaça ball", f"{avg_score:.1f}%"],
    ]
    elements.append(create_table(test_data))
    elements.append(Spacer(1, 15))
    
    # ===== 5. Uy vazifalari holati =====
    hw_qs = HomeworkSubmission.objects.all()
    if profession:
        hw_qs = hw_qs.filter(homework__lesson__profession=profession)
    hw_qs = hw_qs.filter(**date_range('submitted_at'))
    total_submissions = hw_qs.count()
    pending_homeworks = hw_qs.filter(grade__isnull=True).count()
    graded_homeworks = hw_qs.filter(grade__isnull=False).count()
    avg_grade = hw_qs.filter(grade__isnull=False).aggregate(avg=Avg('grade'))['avg'] or 0
    
    elements.append(Paragraph("5. Uy vazifalari holati", heading_style))
    hw_data = [
        ["Körsatkiç", 'Qiymat'],
        ['Jami topşirilgan vazifalar', str(total_submissions)],
        ['Baholanmagan (kutilmoqda)', str(pending_homeworks)],
        ['Baholangan', str(graded_homeworks)],
        ["Örtaça baho", f"{avg_grade:.1f}"],
    ]
    elements.append(create_table(hw_data))
    elements.append(Spacer(1, 15))
    
    # ===== 6. Coin statistikasi (davr böyiça) =====
    coin_tx_qs = CoinTransaction.objects.filter(**date_range('created_at'))
    total_transactions = coin_tx_qs.count()
    total_coins = CustomUser.objects.aggregate(Sum('coins'))['coins__sum'] or 0
    avg_coins_per_user = total_coins / total_users if total_users > 0 else 0
    
    elements.append(Paragraph("6. Coin statistikasi", heading_style))
    coin_data = [
        ["Körsatkiç", 'Qiymat'],
        ['Jami mavjud coinlar', str(total_coins)],
        ['Coin tranzaksiyalari (davr)', str(total_transactions)],
        ["Har bir foydalanuvçiga örtaça", f"{avg_coins_per_user:.1f}"],
    ]
    elements.append(create_table(coin_data))
    elements.append(Spacer(1, 15))
    
    # ===== 7. Kursga yoziliş =====
    enroll_qs = CourseEnrollment.objects.all()
    if profession:
        enroll_qs = enroll_qs.filter(profession=profession)
    enroll_qs = enroll_qs.filter(**date_range('enrolled_at'))
    total_enrollments = enroll_qs.count()
    
    elements.append(Paragraph("7. Kursga yoziliş statistikasi", heading_style))
    enroll_data = [
        ["Körsatkiç", 'Soni'],
        ["Jami yozilişlar", str(total_enrollments)],
    ]
    elements.append(create_table(enroll_data))
    elements.append(Spacer(1, 15))
    
    # ===== 8. Tölovlar (davr böyiça) =====
    pay_qs = PaymentRecord.objects.all()
    if f['year'] and f['month']:
        pay_qs = pay_qs.filter(year=f['year'], month=f['month'])
        pay_label = f"{MONTH_NAMES_UZ[f['month']]} {f['year']}"
    elif f['year']:
        pay_qs = pay_qs.filter(year=f['year'])
        pay_label = str(f['year'])
    elif f['month']:
        pay_qs = pay_qs.filter(month=f['month'], year=today.year)
        pay_label = f"{MONTH_NAMES_UZ[f['month']]} {today.year}"
    else:
        pay_qs = pay_qs.filter(month=today.month, year=today.year)
        pay_label = f"{MONTH_NAMES_UZ[today.month]} {today.year}"
    pay_paid = pay_qs.filter(is_paid=True).count()
    pay_unpaid = pay_qs.filter(is_paid=False).count()
    pay_revenue = pay_qs.aggregate(total=Sum('paid_amount'))['total'] or 0
    
    elements.append(Paragraph(f"8. Tölovlar ({pay_label})", heading_style))
    pay_data = [
        ["Körsatkiç", 'Qiymat'],
        ["Tölangan", str(pay_paid)],
        ["Tölanmagan", str(pay_unpaid)],
        ["Yiğilgan summa", f"{pay_revenue:,.0f} söm"],
    ]
    elements.append(create_table(pay_data))
    elements.append(Spacer(1, 15))
    
    # ===== 9. Oxirgi 7 kunlik registratsiya =====
    reg_base = CustomUser.objects.all()
    if profession:
        reg_base = CustomUser.objects.filter(
            Q(enrollments__profession=profession) | Q(profession=profession)
        ).distinct()
    elements.append(Paragraph("9. Oxirgi 7 kunlik registratsiya", heading_style))
    reg_data = [['Sana', "Yangi foydalanuvçilar"]]
    for i in range(6, -1, -1):
        day = timezone.now().date() - timedelta(days=i)
        count = reg_base.filter(date_joined__date=day).count()
        reg_data.append([day.strftime('%d.%m.%Y'), str(count)])
    elements.append(create_table(reg_data))
    
    doc.build(elements)
    buffer.seek(0)
    
    fname_suffix = f['query_string'].replace('&', '_').replace('=', '') or 'umumiy'
    response = HttpResponse(buffer, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="statistika_{fname_suffix}.pdf"'
    return response


@login_required
def leaderboard(request):
    return render(request, 'accounts/leaderboard.html')


# Messages views
@login_required
def messages_view(request):
    # Şaxsiy xabarlar
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
    
    # Öqilmagan xabarlar sonini yangilaş
    unread_count = all_messages.filter(is_read=False).count()
    
    read_count = personal_messages.filter(is_read=True).count()
    total_count = all_messages.count()
    
    return render(request, 'accounts/messages.html', {
        'messages_list': all_messages,
        'unread_count': unread_count,
        'read_count': read_count,
        'total_count': total_count,
    })


@login_required
def message_detail(request, pk):
    message = get_object_or_404(Message, pk=pk)
    
    # Faqat öziga tegişli xabarlarni köriş
    if message.recipient and message.recipient != request.user:
        if not request.user.is_admin:
            messages.error(request, "Bu xabarga kiriş huquqingiz yöq!")
            return redirect('messages')
    
    # Öqilgan deb belgilaş
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


@login_required
def mark_all_messages_read(request):
    if request.method == 'POST':
        Message.objects.filter(recipient=request.user, is_read=False).update(is_read=True)
        general_q = Q(recipient__isnull=True, is_read=False)
        if request.user.is_student:
            general_q &= Q(message_type__in=['all', 'students'])
        elif request.user.is_teacher:
            general_q &= Q(message_type__in=['all', 'teachers'])
        else:
            general_q &= Q(message_type='all')
        Message.objects.filter(general_q).update(is_read=True)
    return redirect('messages')


@login_required
def delete_read_messages(request):
    if request.method == 'POST':
        mode = request.POST.get('mode', 'read')
        if mode == 'all':
            # Şaxsiy xabarlarni öçiriş
            personal_deleted = Message.objects.filter(recipient=request.user).delete()[0]
            # Umumiy xabarlarni ham öçiriş
            if request.user.is_student:
                general_q = Q(message_type__in=['all', 'students'], recipient__isnull=True)
            elif request.user.is_teacher:
                general_q = Q(message_type__in=['all', 'teachers'], recipient__isnull=True)
            else:
                general_q = Q(message_type='all', recipient__isnull=True)
            general_deleted = Message.objects.filter(general_q).delete()[0]
            total = personal_deleted + general_deleted
            messages.success(request, f"{total} ta xabar öçirildi!")
        else:
            deleted_count = Message.objects.filter(recipient=request.user, is_read=True).delete()[0]
            messages.success(request, f"{deleted_count} ta öqilgan xabar öçirildi!")
    return redirect('messages')


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
            notify_user(
                recipient.telegram_id,
                f"📩 <b>Yangi xabar!</b>\n\n"
                f"📌 {title}\n"
                f"💬 {content[:150]}"
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
                # Barçaga
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
    
    all_messages = Message.objects.all().order_by('-created_at')
    paginator = Paginator(all_messages, 25)
    page = request.GET.get('page')
    all_messages = paginator.get_page(page)
    return render(request, 'accounts/admin/messages.html', {'messages_list': all_messages})

from django.core.paginator import Paginator

# Admin: Payment management






@login_required
def send_payment_reminder(request, pk):
    if not request.user.is_admin:
        return redirect('home')

    student = get_object_or_404(CustomUser, pk=pk, role='student')
    Message.objects.create(
        sender=request.user, recipient=student,
        title="💳 Tölov eslatmasi",
        content=f"Hurmatli {student.full_name},\n\nSizning oylik tölovingiz muddati yaqinlaşmoqda. Iltimos, öz vaqtida tölov qilişni unutmang.\n\nHurmat bilan,\nIT Creative jamoasi",
        message_type='payment'
    )
    notify_user(
        student.telegram_id,
        f"💳 <b>Tölov eslatmasi</b>\n\n"
        f"Hurmatli {student.full_name},\n\n"
        f"Sizning oylik tölovingiz muddati yaqinlaşmoqda.\n"
        f"Iltimos, öz vaqtida tölov qilişni unutmang.\n\n"
        f"🏫 IT Creative jamoasi"
    )
    notify_group(
        f"💳 <b>Tölov eslatmasi yuborildi</b>\n\n"
        f"👤 {student.full_name}ga tölov eslatmasi yuborildi."
    )
    messages.success(request, f"{student.full_name}ga eslatma yuborildi!")
    month = request.GET.get('month', '')
    year = request.GET.get('year', '')
    return redirect(f'/dashboard/tolovlar/?month={month}&year={year}' if month else 'admin_payments')


@login_required
def send_bulk_payment_reminders(request):
    if not request.user.is_admin:
        return redirect('home')

    if request.method == 'POST':
        from datetime import date
        today = date.today()
        m = _to_int(request.POST.get('month'), today.month)
        y = _to_int(request.POST.get('year'), today.year)
        unpaid_ids = PaymentRecord.objects.filter(
            month=m, year=y, is_paid=False
        ).values_list('user_id', flat=True)
        # Şartnoma rejimidagi va kursni tugatgan öquvçilarga oylik tölov
        # eslatmasi yuborilmaydi — ular oylik tölovga aloqador emas.
        unpaid_students = CustomUser.objects.filter(
            pk__in=unpaid_ids, role='student', is_finished=False
        ).exclude(student_balance__is_contract=True)

        count = 0
        for student in unpaid_students:
            Message.objects.create(
                sender=request.user, recipient=student,
                title="💳 Tölov eslatmasi",
                content=f"Hurmatli {student.full_name},\n\nSizning oylik tölovingiz muddati yaqinlaşmoqda. Iltimos, öz vaqtida tölov qilişni unutmang.\n\nHurmat bilan,\nIT Creative jamoasi",
                message_type='payment'
            )
            notify_user(
                student.telegram_id,
                f"💳 <b>Tölov eslatmasi</b>\n\n"
                f"Hurmatli {student.full_name},\n\n"
                f"Sizning oylik tölovingiz muddati yaqinlaşmoqda.\n"
                f"Iltimos, öz vaqtida tölov qilişni unutmang.\n\n"
                f"🏫 IT Creative jamoasi"
            )
            count += 1

        notify_group(
            f"💳 <b>Ommaviy tölov eslatmasi</b>\n\n"
            f"📊 {count} ta öquvçiga tölov eslatmasi yuborildi."
        )
        messages.success(request, f"{count} ta öquvçiga eslatma yuborildi!")

    return redirect(f'/dashboard/tolovlar/?month={m}&year={y}')


@login_required
def admin_toggle_block_payment(request, pk):
    if not request.user.is_admin:
        return redirect('home')

    user = get_object_or_404(CustomUser, pk=pk, role='student')
    payment_status, _ = PaymentStatus.objects.get_or_create(user=user)

    if user.is_blocked:
        user.is_blocked = False
        user.save()
        payment_status.auto_blocked = False
        payment_status.save()
        Message.objects.create(
            title="✅ Hisobingiz oçildi",
            content="Admin hisobingizni oçdi. Öqişingizni davom eting!",
            message_type='system', recipient=user, sender=request.user
        )
        if user.telegram_id:
            notify_user(
                user.telegram_id,
                f"✅ <b>Hisobingiz blokdan çiqarildi!</b>\n\n"
                f"👤 {user.full_name}\n\n"
                f"🎉 Hisobingiz oçildi. Öqişingizni davom eting!"
            )
        messages.success(request, f"{user.full_name} blokdan oçildi!")
    else:
        user.is_blocked = True
        user.save()
        payment_status.auto_blocked = True
        payment_status.save()
        Message.objects.create(
            title="🚫 Hisobingiz bloklandi",
            content="Tölov qilinmaganligi sababli hisobingiz vaqtinça bloklandi.",
            message_type='payment', recipient=user, sender=request.user
        )
        if user.telegram_id:
            notify_user(
                user.telegram_id,
                f"🚫 <b>Hisobingiz bloklandi!</b>\n\n"
                f"👤 {user.full_name}\n\n"
                f"❌ Tölov qilinmaganligi sababli hisobingiz vaqtinça bloklandi.\n"
                f"💳 Tölovni amalga oşiring yoki adminstratsiya bilan boğlaning."
            )
        messages.warning(request, f"{user.full_name} bloklandi!")

    month = request.GET.get('month', '')
    year = request.GET.get('year', '')
    return redirect(f'/dashboard/tolovlar/?month={month}&year={year}' if month else 'admin_payments')


@login_required
def bulk_payment_action(request):
    if not request.user.is_admin:
        return redirect('home')

    if request.method == 'POST':
        from datetime import date
        today = date.today()
        ids_str = request.POST.get('selected_ids', '')
        action = request.POST.get('action', '')
        month = request.POST.get('month', str(today.month))
        year = request.POST.get('year', str(today.year))

        if not ids_str:
            messages.warning(request, "Heç qanday öquvçi tanlanmadi!")
            return redirect(f'/dashboard/tolovlar/?month={month}&year={year}')

        student_ids = [int(x) for x in ids_str.split(',') if x.strip().isdigit()]
        students = CustomUser.objects.filter(pk__in=student_ids, role='student')

        if action == 'block':
            count = 0
            for student in students:
                if not student.is_blocked:
                    student.is_blocked = True
                    student.save()
                    ps, _ = PaymentStatus.objects.get_or_create(user=student)
                    ps.auto_blocked = True
                    ps.save()
                    Message.objects.create(
                        title="🚫 Hisobingiz bloklandi",
                        content="Tölov qilinmaganligi sababli hisobingiz vaqtinça bloklandi.",
                        message_type='payment', recipient=student, sender=request.user
                    )
                    count += 1
            messages.warning(request, f"{count} ta öquvçi bloklandi!")

        elif action == 'unblock':
            count = 0
            for student in students:
                if student.is_blocked:
                    student.is_blocked = False
                    student.save()
                    ps, _ = PaymentStatus.objects.get_or_create(user=student)
                    ps.auto_blocked = False
                    ps.save()
                    Message.objects.create(
                        title="✅ Hisobingiz oçildi",
                        content="Admin hisobingizni oçdi. Öqişingizni davom eting!",
                        message_type='system', recipient=student, sender=request.user
                    )
                    count += 1
            messages.success(request, f"{count} ta öquvçi blokdan oçildi!")

        elif action == 'set_fee':
            fee = _to_int(request.POST.get('monthly_fee'), 200000)
            count = 0
            for student in students:
                ps, _ = PaymentStatus.objects.get_or_create(user=student)
                ps.monthly_fee = fee
                ps.save()
                PaymentRecord.objects.filter(
                    user=student, month=int(month), year=int(year), is_paid=False
                ).update(amount=fee)
                count += 1
            messages.success(request, f"{count} ta öquvçiga {fee:,} söm oylik belgilandi!")

        elif action == 'remind':
            count = 0
            for student in students:
                Message.objects.create(
                    sender=request.user, recipient=student,
                    title="💳 Tölov eslatmasi",
                    content=f"Hurmatli {student.full_name},\n\nSizning oylik tölovingiz muddati yaqinlaşmoqda. Iltimos, öz vaqtida tölov qilişni unutmang.\n\nHurmat bilan,\nIT Creative jamoasi",
                    message_type='payment'
                )
                count += 1
            messages.success(request, f"{count} ta öquvçiga eslatma yuborildi!")

        return redirect(f'/dashboard/tolovlar/?month={month}&year={year}')

    return redirect('admin_payments')


@login_required
def admin_set_monthly_fee(request, pk):
    if not request.user.is_admin:
        return redirect('home')

    user = get_object_or_404(CustomUser, pk=pk, role='student')
    payment_status, _ = PaymentStatus.objects.get_or_create(user=user)

    if request.method == 'POST':
        fee = request.POST.get('monthly_fee', 200000)
        payment_status.monthly_fee = int(fee)
        payment_status.save()

        month = request.GET.get('month', '')
        year = request.GET.get('year', '')
        if month and year:
            PaymentRecord.objects.filter(
                user=user, month=int(month), year=int(year), is_paid=False
            ).update(amount=int(fee))

        messages.success(request, f"{user.full_name} oylik tölovi {int(fee):,} söm qilib belgilandi!")
        return redirect(f'/dashboard/tolovlar/?month={month}&year={year}' if month else 'admin_payments')

    month = request.GET.get('month', '')
    year = request.GET.get('year', '')
    return redirect(f'/dashboard/tolovlar/?month={month}&year={year}' if month else 'admin_payments')




@login_required
def export_payments_pdf(request):
    if not request.user.is_admin:
        return redirect('home')

    from datetime import date
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.units import mm
    from io import BytesIO

    today = date.today()
    selected_month = _to_int(request.GET.get('month'), today.month)
    selected_year = _to_int(request.GET.get('year'), today.year)

    MONTH_NAMES = {
        1: 'Yanvar', 2: 'Fevral', 3: 'Mart', 4: 'Aprel',
        5: 'May', 6: 'Iyun', 7: 'Iyul', 8: 'Avgust',
        9: 'Sentyabr', 10: 'Oktyabr', 11: 'Noyabr', 12: 'Dekabr'
    }
    month_name = MONTH_NAMES.get(selected_month, '')

    records = PaymentRecord.objects.filter(
        month=selected_month, year=selected_year
    ).select_related('user', 'user__payment_status')

    total_expected = sum(r.amount for r in records)
    total_paid = sum(r.paid_amount for r in records if r.is_paid)
    total_debt = total_expected - total_paid

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), topMargin=20, bottomMargin=20, leftMargin=20, rightMargin=20)
    elements = []
    styles = getSampleStyleSheet()

    heading = ParagraphStyle('PH', parent=styles['Heading2'], textColor=colors.HexColor('#0d6efd'), spaceAfter=6)

    elements.append(Paragraph(f"Tölovlar hisoboti — {month_name} {selected_year}", styles['Title']))
    elements.append(Paragraph(f"Hisobot sanasi: {today.strftime('%d.%m.%Y')}", styles['Normal']))
    elements.append(Spacer(1, 12))

    # Summary table
    elements.append(Paragraph("Umumiy ma'lumot", heading))
    summary_data = [
        ["Körsatkiç", 'Qiymat'],
        ['Jami öquvçilar', str(records.count())],
        ['Tölangan', str(records.filter(is_paid=True).count())],
        ['Tölanmagan', str(records.filter(is_paid=False).count())],
        ['Kutilgan summa', f"{total_expected:,.0f} söm"],
        ['Yiğilgan summa', f"{total_paid:,.0f} söm"],
        ['Jami qarz', f"{total_debt:,.0f} söm"],
    ]
    t = Table(summary_data, colWidths=[200, 180])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0d6efd')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#dee2e6')),
        ('PADDING', (0, 0), (-1, -1), 8),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8f9fa')]),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 16))

    # Detail table
    elements.append(Paragraph("Batafsil tölov jadvali", heading))
    detail_data = [['#', 'Ism Familiya', 'Telefon', 'Oylik tölov', 'Tölangan', 'Qarz', 'Holat', 'Sana', 'Izoh']]
    col_widths = [25, 130, 90, 80, 80, 80, 70, 70, 120]

    for i, r in enumerate(records.order_by('user__first_name', 'user__last_name'), 1):
        detail_data.append([
            str(i),
            r.user.full_name,
            r.user.phone or '',
            f"{r.amount:,.0f}",
            f"{r.paid_amount:,.0f}" if r.is_paid else '0',
            f"{max(0, r.amount - r.paid_amount):,.0f}" if not r.is_paid else '0',
            'Tölangan' if r.is_paid else 'Tölanmagan',
            r.paid_date.strftime('%d.%m.%Y') if r.paid_date else '-',
            (r.notes or '')[:30],
        ])

    dt = Table(detail_data, colWidths=col_widths)
    dt.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0d6efd')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#dee2e6')),
        ('PADDING', (0, 0), (-1, -1), 6),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8f9fa')]),
        ('ALIGN', (0, 0), (0, -1), 'CENTER'),
        ('ALIGN', (3, 0), (5, -1), 'RIGHT'),
        ('ALIGN', (6, 0), (6, -1), 'CENTER'),
    ]))

    # Color paid/unpaid rows
    for i, r in enumerate(records.order_by('user__first_name', 'user__last_name'), 1):
        if r.is_paid:
            dt.setStyle(TableStyle([('BACKGROUND', (6, i), (6, i), colors.HexColor('#d1e7dd'))]))
        else:
            dt.setStyle(TableStyle([('BACKGROUND', (6, i), (6, i), colors.HexColor('#f8d7da'))]))

    elements.append(dt)
    doc.build(elements)
    buffer.seek(0)

    response = HttpResponse(buffer, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="tolovlar_{month_name}_{selected_year}.pdf"'
    return response


# Teacher/Admin: Manage lessons
@login_required
def manage_lessons(request, pk):
    if not (request.user.is_admin or request.user.is_teacher):
        messages.error(request, "Sizda bu sahifaga kiriş huquqi yöq!")
        return redirect('home')
    
    profession = get_object_or_404(Profession, pk=pk)
    
    if request.user.is_teacher and not request.user.teaching_professions.filter(pk=profession.pk).exists():
        messages.error(request, "Sizda bu kursga kiriş huquqi yöq!")
        return redirect('professions')
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
    
    if request.user.is_teacher and not request.user.teaching_professions.filter(pk=profession.pk).exists():
        messages.error(request, "Sizda bu kursga kiriş huquqi yöq!")
        return redirect('professions')

    # ─── Mavzu (dars) limiti ───
    # Kurs uçun topic_count belgilangan bölsa, undan ortiq dars qöşib bölmaydi.
    if profession.is_lesson_limit_reached:
        messages.error(
            request,
            f"Mavzu (dars) limiti töldi: {profession.topic_count} tadan {profession.lessons.count()} ta dars qöşilgan. "
            f"Yangi dars qöşib bölmaydi. Limitni oşiriş uçun kursni tahrirlang."
        )
        return redirect('profession_detail', pk=pk)

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
                vimeo_url = form.cleaned_data.get('vimeo_url') or ''
                youtube_url = form.cleaned_data.get('youtube_url') or ''
                VideoLesson.objects.create(
                    lesson=lesson,
                    video_url=vimeo_url or None,
                    youtube_url=youtube_url or None,
                    # 0 = server YouTube/Vimeo'dan özi aniqlaydi.
                    duration=form.cleaned_data.get('duration') or 0,
                )
                # AI maqola yaratiş
                ai_article = _generate_ai_article(lesson.title, profession.name)
                if ai_article:
                    lesson.ai_article = ai_article
                    lesson.save(update_fields=['ai_article'])
                # Xabar yuboriş
                send_course_notification(
                    profession=profession,
                    title=f"🎬 Yangi video dars: {lesson.title}",
                    content=f"'{profession.name}' kursiga yangi video dars qöşildi:\n\n📚 {lesson.title}\n\nDarsni köriş uçun kursga kiring!",
                    sender=request.user
                )
                messages.success(request, "Video darslik qöşildi!")
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
                    description=form.cleaned_data['description'],
                    max_score=10,
                    ai_check=form.cleaned_data.get('ai_check', False),
                    handwritten_only=form.cleaned_data.get('handwritten_only', False),
                )
                # Xabar yuboriş
                send_course_notification(
                    profession=profession,
                    title=f"📝 Yangi uyga vazifa: {lesson.title}",
                    content=f"'{profession.name}' kursiga yangi uyga vazifa qöşildi:\n\n📚 {lesson.title}\n\nVazifani bajariş uçun kursga kiring!",
                    sender=request.user
                )
                messages.success(request, "Uyga vazifa qöşildi!")
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
                is_final = form.cleaned_data.get('test_type') == 'final'
                quiz = Quiz.objects.create(
                    lesson=lesson,
                    title=form.cleaned_data['title'],
                    status=Quiz.Status.DRAFT,
                    created_by=request.user,
                )
                QuizSettings.objects.create(
                    quiz=quiz,
                    time_limit_minutes=form.cleaned_data['time_limit'],
                    passing_score=form.cleaned_data['passing_score'],
                    allow_retry=True,
                    attempts_allowed=0,
                    shuffle_questions=form.cleaned_data.get('shuffle_questions', False),
                    shuffle_answers=form.cleaned_data.get('shuffle_answers', False),
                    question_count=form.cleaned_data.get('question_count') or 0,
                    retry_payment_required=is_final,
                )
                send_course_notification(
                    profession=profession,
                    title=f"Quiz: {lesson.title}",
                    content=f"'{profession.name}' kursiga yangi quiz qöşildi:\n\n{lesson.title}\n\nQuizni yeçiş uçun kursga kiring!",
                    sender=request.user
                )
                messages.success(request, "Quiz qöşildi! Endi savollarni qöşing.")
                return redirect('quizui:quiz_edit', pk=quiz.pk)
        
        elif lesson_type == 'dictionary':
            form = DictionaryForm(request.POST)
            if form.is_valid():
                lesson = Lesson.objects.create(
                    profession=profession,
                    section=section,
                    title=form.cleaned_data['title'],
                    lesson_type='dictionary',
                    created_by=request.user
                )
                Dictionary.objects.create(
                    lesson=lesson,
                    description=form.cleaned_data.get('description') or ''
                )
                send_course_notification(
                    profession=profession,
                    title=f"📖 Yangi luğat: {lesson.title}",
                    content=f"'{profession.name}' kursiga yangi luğat qöşildi:\n\n📚 {lesson.title}\n\nLuğatni köriş uçun kursga kiring!",
                    sender=request.user
                )
                messages.success(request, "Luğat qöşildi! Endi sözlarni qöşing.")
                return redirect('manage_dictionary', pk=lesson.dictionary.pk)
        
        elif lesson_type == 'speaking':
            title = request.POST.get('title', '').strip()
            description = request.POST.get('description', '').strip()
            target_level = request.POST.get('target_level', 'beginner')
            max_duration = request.POST.get('max_duration', 120)
            max_score = request.POST.get('max_score', 100)
            ai_check = request.POST.get('ai_check') == 'on'
            criteria = request.POST.get('criteria', '').strip()
            due_date = request.POST.get('due_date')
            
            if not title:
                messages.error(request, "Vazifa nomini kiriting.")
                return redirect(request.get_full_path())
            if not description:
                messages.error(request, "Vazifa tavsifini kiriting.")
                return redirect(request.get_full_path())
            
            try:
                max_duration = int(max_duration)
                max_score = int(max_score)
            except (ValueError, TypeError):
                max_duration = 120
                max_score = 100
            
            lesson = Lesson.objects.create(
                profession=profession,
                section=section,
                title=title,
                lesson_type='speaking',
                created_by=request.user,
            )
            
            from accounts.models import SpeakingTask
            SpeakingTask.objects.create(
                lesson=lesson,
                description=description,
                target_level=target_level,
                max_duration=max_duration,
                max_score=max_score,
                ai_check=ai_check,
                criteria=criteria if criteria else None,
                due_date=due_date if due_date else None,
            )
            
            send_course_notification(
                profession=profession,
                title=f"🎤 Yangi Speaking vazifa: {title}",
                content=f"'{profession.name}' kursiga yangi Speaking vazifa qöşildi:\n\n🎤 {title}\n\nVazifani bajariş uçun kursga kiring!",
                sender=request.user
            )
            messages.success(request, f"'{title}' speaking vazifasi yaratildi!")
            return redirect('profession_detail', pk=pk)
    else:
        if lesson_type == 'video':
            form = VideoLessonForm()
        elif lesson_type == 'homework':
            form = HomeworkForm()
        elif lesson_type == 'dictionary':
            form = DictionaryForm()
        elif lesson_type == 'speaking':
            form = None
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
    
    if request.user.is_teacher and not request.user.teaching_professions.filter(pk=lesson.profession.pk).exists():
        messages.error(request, "Sizda bu kursga kiriş huquqi yöq!")
        return redirect('professions')
    
    if request.method == 'POST':
        if lesson.lesson_type == 'video':
            form = VideoLessonForm(request.POST)
            if form.is_valid():
                lesson.title = form.cleaned_data['title']
                lesson.ai_article = request.POST.get('ai_article', '').strip() or None
                lesson.save(update_fields=['title', 'ai_article'])
                new_vimeo = form.cleaned_data.get('vimeo_url') or None
                new_youtube = form.cleaned_data.get('youtube_url') or None
                url_changed = (lesson.video.video_url or None) != new_vimeo or \
                              (lesson.video.youtube_url or None) != new_youtube
                lesson.video.video_url = new_vimeo
                lesson.video.youtube_url = new_youtube

                manual_duration = form.cleaned_data.get('duration') or 0
                if url_changed:
                    # Boşqa video — eski davomiylik va u asosida qulflangan
                    # köriş talabi endi töğri kelmaydi, hammasini tozalaymiz.
                    lesson.video.duration = manual_duration
                    lesson.video.verified_duration_seconds = 0
                    lesson.video.duration_checked_at = None
                    VideoProgress.objects.filter(
                        video=lesson.video, watched=False
                    ).update(required_seconds=0)
                elif manual_duration:
                    lesson.video.duration = manual_duration
                lesson.video.save()
                # Xabar yuboriş
                send_course_notification(
                    profession=lesson.profession,
                    title=f"🔄 Video dars yangilandi: {lesson.title}",
                    content=f"'{lesson.profession.name}' kursidagi video dars yangilandi:\n\n📚 {lesson.title}\n\nYangilangan darsni köriş uçun kursga kiring!",
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
                lesson.homework.max_score = 10
                lesson.homework.ai_check = form.cleaned_data.get('ai_check', False)
                lesson.homework.handwritten_only = form.cleaned_data.get('handwritten_only', False)
                lesson.homework.save()
                # Xabar yuboriş
                send_course_notification(
                    profession=lesson.profession,
                    title=f"🔄 Uyga vazifa yangilandi: {lesson.title}",
                    content=f"'{lesson.profession.name}' kursidagi uyga vazifa yangilandi:\n\n📚 {lesson.title}\n\nVazifani köriş uçun kursga kiring!",
                    sender=request.user
                )
                messages.success(request, "Vazifa yangilandi!")
                return redirect('manage_lessons', pk=lesson.profession.pk)
    else:
        if lesson.lesson_type == 'video':
            form = VideoLessonForm(initial={
                'title': lesson.title,
                'vimeo_url': lesson.video.video_url or '',
                'youtube_url': lesson.video.youtube_url or '',
                'duration': lesson.video.duration or None,
            })
        elif lesson.lesson_type == 'homework':
            form = HomeworkForm(initial={
                'title': lesson.title,
                'description': lesson.homework.description,
                'ai_check': lesson.homework.ai_check,
                'handwritten_only': lesson.homework.handwritten_only,
            })
        elif lesson.lesson_type == 'dictionary':
            if hasattr(lesson, 'dictionary') and lesson.dictionary:
                return redirect('manage_dictionary', pk=lesson.dictionary.pk)
            else:
                return redirect('manage_lessons', pk=lesson.profession.pk)
        else:
            quiz = getattr(lesson, 'quiz', None)
            if quiz:
                return redirect('quizui:quiz_edit', pk=quiz.pk)
            else:
                return redirect('manage_lessons', pk=lesson.profession.pk)
    
    return render(request, 'accounts/manage/edit_lesson.html', {
        'lesson': lesson,
        'form': form,
    })


@login_required
def delete_lesson(request, pk):
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')
    
    lesson = get_object_or_404(Lesson, pk=pk)
    
    if request.user.is_teacher and not request.user.teaching_professions.filter(pk=lesson.profession.pk).exists():
        messages.error(request, "Sizda bu kursga kiriş huquqi yöq!")
        return redirect('professions')
    profession = lesson.profession
    profession_pk = profession.pk
    lesson_title = lesson.title
    lesson_type = lesson.lesson_type
    
    if request.method == 'POST':
        # Xabar yuboriş
        type_names = {'video': 'Video dars', 'homework': 'Uyga vazifa', 'test': 'Test', 'dictionary': "Luğat"}
        type_name = type_names.get(lesson_type, 'Dars')
        send_course_notification(
            profession=profession,
            title=f"🗑️ {type_name} öçirildi",
            content=f"'{profession.name}' kursidan quyidagi dars öçirildi:\n\n📚 {lesson_title}\n\nBu dars endi mavjud emas.",
            sender=request.user
        )
        lesson.delete()
        messages.success(request, "Dars öçirildi!")
        return redirect('manage_lessons', pk=profession_pk)
    
    return render(request, 'accounts/manage/delete_lesson.html', {'lesson': lesson})


# Test management
@login_required
def manage_test_questions(request, pk):
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')

    test = get_object_or_404(Test, pk=pk)
    questions = test.questions.all()

    ai_blocked = (
        request.user.is_teacher
        and not getattr(request.user, 'ai_access_enabled', True)
    )

    return render(request, 'accounts/manage/test_questions.html', {
        'test': test,
        'questions': questions,
        'ai_blocked': ai_blocked,
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
            
            messages.success(request, "Savol qöşildi!")
            
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
        messages.success(request, "Savol öçirildi!")
    
    return redirect('manage_test_questions', pk=test_pk)


@login_required
def import_test_questions(request, pk):
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')

    test = get_object_or_404(Test, pk=pk)
    errors = []
    success_count = 0

    if request.method == 'POST':
        file = request.FILES.get('excel_file')

        if not file:
            errors.append("Fayl tanlanmadi.")
        elif not file.name.endswith('.xlsx'):
            errors.append("Faqat .xlsx formatdagi fayllar qabul qilinadi.")
        else:
            try:
                import openpyxl
            except ImportError:
                errors.append("openpyxl kutubxonasi örnatilmagan. pip install openpyxl")

            if not errors:
                try:
                    wb = openpyxl.load_workbook(file, read_only=True)
                    ws = wb.active

                    headers = [to_new_alphabet(str(cell.value or '').strip().lower()) for cell in next(ws.iter_rows(min_row=1, max_row=1))]

                    required_cols = {
                        'savol': None,
                        'a': None,
                        'b': None,
                        'c': None,
                        "töğri javob": None,
                    }
                    explanation_col = None

                    for i, h in enumerate(headers):
                        if h in ('savol', 'question_text', 'question', 'savol matni'):
                            required_cols['savol'] = i
                        elif h in ('a', 'option_a', 'variant_a', 'javob_a', '1-javob'):
                            required_cols['a'] = i
                        elif h in ('b', 'option_b', 'variant_b', 'javob_b', '2-javob'):
                            required_cols['b'] = i
                        elif h in ('c', 'option_c', 'variant_c', 'javob_c', '3-javob'):
                            required_cols['c'] = i
                        elif h in ("töğri javob", 'correct_option', 'correct', 'javob', "töğri"):
                            required_cols["töğri javob"] = i
                        elif h in ('izoh', 'explanation', 'tuşuntiriş'):
                            explanation_col = i

                    missing = [k for k, v in required_cols.items() if v is None]
                    if missing:
                        errors.append(f"Excel faylda quyidagi ustunlar topilmadi: {', '.join(missing)}")
                        errors.append(f"Topilgan ustunlar: {', '.join(headers)}")

                    if not errors:
                        questions_data = []

                        for row_num, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
                            row = list(row)
                            max_col = max(v for v in required_cols.values() if v is not None)
                            if explanation_col is not None:
                                max_col = max(max_col, explanation_col)
                            if len(row) <= max_col:
                                row.extend([None] * (max_col - len(row) + 1))

                            q_text = str(row[required_cols['savol']] or '').strip()
                            opt_a = str(row[required_cols['a']] or '').strip()
                            opt_b = str(row[required_cols['b']] or '').strip()
                            opt_c = str(row[required_cols['c']] or '').strip()
                            correct = str(row[required_cols["töğri javob"]] or '').strip().upper()

                            explanation = ''
                            if explanation_col is not None:
                                explanation = str(row[explanation_col] or '').strip()

                            if not q_text and not opt_a and not opt_b and not opt_c and not correct:
                                continue

                            row_errors = []

                            if not q_text:
                                row_errors.append("savol böş")
                            if not opt_a:
                                row_errors.append("A varianti böş")
                            if not opt_b:
                                row_errors.append("B varianti böş")
                            if not opt_c:
                                row_errors.append("C varianti böş")
                            if correct not in ('A', 'B', 'C'):
                                row_errors.append(f"töğri javob '{correct}' notöğri (faqat A/B/C)")

                            if row_errors:
                                errors.append(f"Qator {row_num}: {', '.join(row_errors)}")
                            else:
                                questions_data.append({
                                    'question_text': q_text,
                                    'option_a': opt_a,
                                    'option_b': opt_b,
                                    'option_c': opt_c,
                                    'correct': correct,
                                    'explanation': explanation,
                                })

                        if not questions_data and not errors:
                            errors.append("Excelda heç qanday savol topilmadi.")

                        if not errors:
                            existing_texts = set(
                                test.questions.values_list('question_text', flat=True)
                            )
                            seen_texts = set()
                            unique_data = []
                            skipped = 0
                            for qd in questions_data:
                                qt = qd['question_text']
                                if qt in existing_texts or qt in seen_texts:
                                    skipped += 1
                                    continue
                                seen_texts.add(qt)
                                unique_data.append(qd)

                            with transaction.atomic():
                                existing_count = test.questions.count()
                                for i, qd in enumerate(unique_data):
                                    question = TestQuestion.objects.create(
                                        test=test,
                                        question_text=qd['question_text'],
                                        explanation=qd.get('explanation', ''),
                                        order=existing_count + i + 1,
                                    )
                                    options = [
                                        ('A', qd['option_a']),
                                        ('B', qd['option_b']),
                                        ('C', qd['option_c']),
                                    ]
                                    for letter, text in options:
                                        TestAnswer.objects.create(
                                            question=question,
                                            answer_text=text,
                                            is_correct=(letter == qd['correct']),
                                        )
                                success_count = len(unique_data)
                            msg = f"{success_count} ta savol muvaffaqiyatli import qilindi!"
                            if skipped:
                                msg += f" ({skipped} ta takroriy savol ötkazib yuborildi)"
                            messages.success(request, msg)
                            return redirect('manage_test_questions', pk=test.pk)

                    wb.close()
                except Exception as e:
                    errors.append(f"Faylni öqişda xatolik: {str(e)}")

    return render(request, 'accounts/manage/import_questions.html', {
        'test': test,
        'errors': errors,
        'success_count': success_count,
    })


@login_required
def manage_dictionary(request, pk):
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')
    dictionary = get_object_or_404(Dictionary, pk=pk)
    words = dictionary.words.all()
    return render(request, 'accounts/manage/manage_dictionary.html', {
        'dictionary': dictionary,
        'words': words,
    })


@login_required
def add_dictionary_word(request, pk):
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')
    dictionary = get_object_or_404(Dictionary, pk=pk)
    if request.method == 'POST':
        term = request.POST.get('term', '').strip()
        translation = request.POST.get('translation', '').strip()
        example = request.POST.get('example', '').strip()
        if term and translation:
            max_order = dictionary.words.count()
            DictionaryWord.objects.create(
                dictionary=dictionary,
                term=term,
                translation=translation,
                example=example or None,
                order=max_order + 1,
            )
            messages.success(request, f"'{term}' sözi qöşildi!")
        else:
            messages.error(request, "Söz va tarjimani kiriting!")
    return redirect('manage_dictionary', pk=pk)


@login_required
def delete_dictionary_word(request, pk):
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')
    word = get_object_or_404(DictionaryWord, pk=pk)
    dict_pk = word.dictionary.pk
    if request.method == 'POST':
        word.delete()
        messages.success(request, "Söz öçirildi!")
    return redirect('manage_dictionary', pk=dict_pk)


@login_required
def import_dictionary_words(request, pk):
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')
    dictionary = get_object_or_404(Dictionary, pk=pk)
    errors = []
    success_count = 0

    if request.method == 'POST':
        file = request.FILES.get('excel_file')
        if not file:
            errors.append("Fayl tanlanmadi.")
        elif not file.name.endswith('.xlsx'):
            errors.append("Faqat .xlsx formatdagi fayllar qabul qilinadi.")
        else:
            try:
                import openpyxl
            except ImportError:
                errors.append("openpyxl kutubxonasi örnatilmagan.")

            if not errors:
                try:
                    wb = openpyxl.load_workbook(file, read_only=True)
                    ws = wb.active
                    headers = [to_new_alphabet(str(cell.value or '').strip().lower()) for cell in next(ws.iter_rows(min_row=1, max_row=1))]

                    term_col = None
                    translation_col = None
                    example_col = None

                    for i, h in enumerate(headers):
                        if h in ("söz", 'soz', 'term', 'atama', 'word', "söz / atama", 'yozilişi'):
                            term_col = i
                        elif h in ('tarjima', 'translation', "ma'no", "ma'nosi", "tarjima / ma'nosi", 'tarjimasi'):
                            translation_col = i
                        elif h in ('misol', 'example', 'namuna', 'gap'):
                            example_col = i

                    if term_col is None:
                        errors.append("'Söz' ustuni topilmadi. Ustun nomi: söz, term, atama, word")
                    if translation_col is None:
                        errors.append("'Tarjima' ustuni topilmadi. Ustun nomi: tarjima, translation, ma'no")
                    if errors:
                        errors.append(f"Topilgan ustunlar: {', '.join(headers)}")

                    if not errors:
                        words_data = []
                        max_col = max(c for c in [term_col, translation_col, example_col] if c is not None)
                        for row_num, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
                            row = list(row)
                            if len(row) <= max_col:
                                row.extend([None] * (max_col - len(row) + 1))

                            term = str(row[term_col] or '').strip()
                            translation = str(row[translation_col] or '').strip()
                            example = ''
                            if example_col is not None:
                                example = str(row[example_col] or '').strip()

                            if not term and not translation:
                                continue

                            if not term:
                                errors.append(f"Qator {row_num}: söz böş")
                            elif not translation:
                                errors.append(f"Qator {row_num}: tarjima böş")
                            else:
                                words_data.append({
                                    'term': term,
                                    'translation': translation,
                                    'example': example,
                                })

                        if not words_data and not errors:
                            errors.append("Excelda heç qanday söz topilmadi.")

                        if not errors:
                            with transaction.atomic():
                                existing_count = dictionary.words.count()
                                for i, wd in enumerate(words_data):
                                    DictionaryWord.objects.create(
                                        dictionary=dictionary,
                                        term=wd['term'],
                                        translation=wd['translation'],
                                        example=wd['example'] or None,
                                        order=existing_count + i + 1,
                                    )
                                success_count = len(words_data)
                            messages.success(request, f"{success_count} ta söz muvaffaqiyatli import qilindi!")
                            return redirect('manage_dictionary', pk=dictionary.pk)

                    wb.close()
                except Exception as e:
                    errors.append(f"Faylni öqişda xatolik: {str(e)}")

    return render(request, 'accounts/manage/import_dictionary.html', {
        'dictionary': dictionary,
        'errors': errors,
        'success_count': success_count,
    })


@login_required
def ai_generate_dictionary(request, pk):
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')

    if request.user.is_teacher and not getattr(request.user, 'ai_access_enabled', True):
        messages.error(request, "AI funksiyasi sizga admin tomonidan bloklangan. Bu funksiyadan foydalaniş uçun admin bilan boğlaning.")
        return redirect('manage_dictionary', pk=pk)

    dictionary = get_object_or_404(Dictionary, pk=pk)

    if request.method != 'POST':
        messages.error(request, "Notöğri sörov turi.")
        return redirect('manage_dictionary', pk=dictionary.pk)

    language = request.POST.get('language', '').strip()
    level = request.POST.get('level', '').strip()
    try:
        count = int(request.POST.get('count', 10))
    except (ValueError, TypeError):
        count = 10
    count = min(count, 50)

    if not language or not level:
        messages.error(request, "Til va daraja körsatilişi şart.")
        return redirect('manage_dictionary', pk=dictionary.pk)

    from django.conf import settings
    api_key = getattr(settings, 'OPENAI_API_KEY', None)
    if not api_key or not api_key.strip():
        messages.error(request, "AI xizmati sozlanmagan (OPENAI_API_KEY topilmadi)")
        return redirect('manage_dictionary', pk=dictionary.pk)

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key.strip(), timeout=120)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Siz tajribali til öqituvçisisiz. "
                        "Faqat JSON array formatida javob bering, boşqa heç qanday matn qöşmang. "
                        "Har bir element quyidagi formatda bölsin:\n"
                        '{"term": "maqsad tildagi söz", '
                        '"translation": "Özbek tilidagi tarjimasi", '
                        '"example": "Maqsad tildagi misol gap | Özbek tilidagi tarjimasi"}'
                    ),
                },
                {
                    "role": "user",
                    "content": f"{language} tilidan {level} darajada {count} ta söz yarat.",
                },
            ],
            max_tokens=4000,
            temperature=0.7,
        )
        ai_text = response.choices[0].message.content.strip()

        if ai_text.startswith('```'):
            lines = ai_text.split('\n')
            lines = [l for l in lines if not l.strip().startswith('```')]
            ai_text = '\n'.join(lines)

        words_data = json.loads(ai_text)

        if not isinstance(words_data, list):
            messages.error(request, "AI notöğri formatda javob qaytardi.")
            return redirect('manage_dictionary', pk=dictionary.pk)

        with transaction.atomic():
            existing_count = dictionary.words.count()
            for i, wd in enumerate(words_data):
                DictionaryWord.objects.create(
                    dictionary=dictionary,
                    term=wd.get('term', ''),
                    translation=wd.get('translation', ''),
                    example=wd.get('example', '') or None,
                    order=existing_count + i + 1,
                )

        messages.success(request, f"AI yordamida {len(words_data)} ta söz muvaffaqiyatli yaratildi!")
    except json.JSONDecodeError:
        messages.error(request, "AI javobini öqişda xatolik yuz berdi. Qaytadan urinib köring.")
    except Exception as e:
        messages.error(request, safe_error(e, 'ai_generate_words', "AI söz yaratişda xatolik yuz berdi."))

    return redirect('manage_dictionary', pk=dictionary.pk)


# Homework submissions for teachers
@login_required
def homework_submissions(request):
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')
    
    search_query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '')
    profession_filter = request.GET.get('profession', '')
    sort_by = request.GET.get('sort', '')
    
    submissions = HomeworkSubmission.objects.select_related(
        'homework__lesson__profession', 'student'
    ).prefetch_related('messages')
    
    # Öqituvçi faqat öz kurslaridagi vazifalarni körsin
    if request.user.is_teacher and not request.user.is_admin:
        teaching_ids = request.user.teaching_professions.values_list('pk', flat=True)
        submissions = submissions.filter(homework__lesson__profession_id__in=teaching_ids)
    
    if search_query:
        from django.db.models import Q
        submissions = submissions.filter(
            Q(student__first_name__icontains=search_query) |
            Q(student__last_name__icontains=search_query) |
            Q(student__username__icontains=search_query) |
            Q(homework__lesson__title__icontains=search_query)
        )
    
    if status_filter == 'ai_checked':
        submissions = submissions.exclude(ai_feedback__isnull=True).exclude(ai_feedback='')
    elif status_filter:
        submissions = submissions.filter(status=status_filter)
    else:
        submissions = submissions.exclude(status='graded')
    
    if profession_filter:
        submissions = submissions.filter(homework__lesson__profession_id=profession_filter)
    
    if sort_by == 'oldest':
        submissions = submissions.order_by('submitted_at')
    elif sort_by == 'name':
        submissions = submissions.order_by('student__first_name', 'student__last_name')
    else:
        submissions = submissions.order_by('-submitted_at')
    
    total_count = submissions.count()
    
    paginator = Paginator(submissions, 25)
    page = request.GET.get('page')
    submissions = paginator.get_page(page)
    
    # Öqituvçi uçun faqat öz kurslari böyiça statistika
    if request.user.is_teacher and not request.user.is_admin:
        hw_qs = HomeworkSubmission.objects.filter(homework__lesson__profession_id__in=teaching_ids)
        stats = {
            'pending': hw_qs.filter(status='pending').count(),
            'reviewing': hw_qs.filter(status='reviewing').count(),
            'revision': hw_qs.filter(status='revision').count(),
            'accepted': hw_qs.filter(status='accepted').count(),
            'graded': hw_qs.filter(status='graded').count(),
            'ai_checked': hw_qs.exclude(ai_feedback__isnull=True).exclude(ai_feedback='').count(),
        }
        professions = Profession.objects.filter(pk__in=teaching_ids)
    else:
        stats = {
            'pending': HomeworkSubmission.objects.filter(status='pending').count(),
            'reviewing': HomeworkSubmission.objects.filter(status='reviewing').count(),
            'revision': HomeworkSubmission.objects.filter(status='revision').count(),
            'accepted': HomeworkSubmission.objects.filter(status='accepted').count(),
            'graded': HomeworkSubmission.objects.filter(status='graded').count(),
            'ai_checked': HomeworkSubmission.objects.exclude(ai_feedback__isnull=True).exclude(ai_feedback='').count(),
        }
        professions = Profession.objects.all()
    
    # Kurslar böyiça vazifalar sonini hisoblaş
    # Faqat uyga vazifasi bor kurslarni körsatiş
    if not professions.exists():
        profession_ids = HomeworkSubmission.objects.values_list(
            'homework__lesson__profession_id', flat=True
        ).distinct()
        professions = Profession.objects.filter(pk__in=profession_ids)
    
    profession_stats = []
    for prof in professions.prefetch_related('teachers'):
        base_qs = HomeworkSubmission.objects.filter(homework__lesson__profession=prof)
        p_pending = base_qs.filter(status='pending').count()
        p_total = base_qs.exclude(status='graded').count()
        profession_stats.append({
            'profession': prof,
            'total': p_total,
            'pending': p_pending,
            'teachers': [
                t.get_full_name() or t.username
                for t in prof.teachers.all() if t.role == 'teacher'
            ],
        })
    
    # AI ruxsatini tekşiriş: admin har doim, öqituvçi faqat ai_access_enabled bölsa
    if request.user.is_admin:
        user_ai_enabled = True
    else:
        user_ai_enabled = getattr(request.user, 'ai_access_enabled', False)

    # Admin uçun: ustozga eslatma şablonlari (kurs tanlangan bölsa)
    reminder_templates = []
    course_teachers = []
    if request.user.is_admin and profession_filter:
        try:
            _prof = Profession.objects.prefetch_related('teachers').get(pk=profession_filter)
            course_teachers = [
                t.get_full_name() or t.username
                for t in _prof.teachers.all() if t.role == 'teacher'
            ]
            _pending_qs = HomeworkSubmission.objects.filter(
                homework__lesson__profession_id=profession_filter,
                status='pending',
            )
            _pending_count = _pending_qs.count()
            _oldest = _pending_qs.order_by('submitted_at').first()
            _oldest_hours = 0
            if _oldest:
                _oldest_hours = int(
                    (timezone.now() - _oldest.submitted_at).total_seconds() // 3600
                )
            reminder_templates = _teacher_reminder_templates(
                _prof.name, _pending_count, _oldest_hours
            )
        except (Profession.DoesNotExist, ValueError):
            reminder_templates = []

    return render(request, 'accounts/manage/homework_submissions.html', {
        'submissions': submissions,
        'stats': stats,
        'professions': professions,
        'profession_stats': profession_stats,
        'search_query': search_query,
        'status_filter': status_filter,
        'profession_filter': profession_filter,
        'sort_by': sort_by,
        'total_count': total_count,
        'user_ai_enabled': user_ai_enabled,
        'reminder_templates': reminder_templates,
        'course_teachers': course_teachers,
    })


def _run_admin_ai_grade(submission_pk, grader_pk):
    """Admin "AI bilan baholaş" tugmasi — fon oqimida (ai_yordamchi.jobs.submit) işlaydi."""
    try:
        submission = HomeworkSubmission.objects.select_related(
            'homework__lesson', 'student').filter(pk=submission_pk).first()
        if not submission:
            return
        student_answer = _read_submission_content(submission)
        ai_result = _get_ai_grade(submission.homework.description, student_answer) if student_answer else None
        if not ai_result:
            return

        submission.grade = ai_result['grade']
        submission.feedback = f"🤖 AI xulosasi:\n{ai_result['feedback']}"
        submission.status = ai_result['status']
        submission.graded_by_id = grader_pk
        submission.graded_at = timezone.now()
        submission.save()

        if ai_result['status'] == 'graded' and not submission.coin_awarded and submission.revision_count == 0:
            coin_amount = submission.calculate_coin_reward()
            submission.student.add_coins(coin_amount, f"Vazifa baholandi: {submission.homework.lesson.title}")
            submission.coin_awarded = True
            submission.coins_earned = coin_amount
            submission.save(update_fields=['coin_awarded', 'coins_earned'])
            ActivityLog.objects.create(
                user=submission.student,
                action_type='homework_graded',
                description=f"Vazifa AI baholadi: {submission.homework.lesson.title} - Baho: {submission.grade}, +{coin_amount} coin"
            )

        if ai_result['status'] == 'graded':
            _notify_ai_graded(submission, ai_result)
        else:
            Message.objects.create(
                title="Vazifani qayta işlang",
                content=f"'{submission.homework.lesson.title}' vazifangizni qayta işlaşingiz kerak.\n\nAI izohi: {ai_result['feedback']}",
                message_type='warning',
                recipient=submission.student
            )
            _notify_ai_revision(submission, ai_result)
    finally:
        cache.delete(f'ai-grade-hw:{submission_pk}')


@login_required
def grade_homework(request, pk):
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')
    
    submission = get_object_or_404(HomeworkSubmission, pk=pk)
    
    if request.user.is_teacher and not request.user.teaching_professions.filter(pk=submission.homework.lesson.profession.pk).exists():
        messages.error(request, "Sizda bu kursga kiriş huquqi yöq!")
        return redirect('professions')
    was_graded = submission.grade is not None
    chat_messages = submission.messages.all().order_by('created_at')
    revisions = submission.revisions.all()
    
    # Xabarlarni öqilgan deb belgilaş
    submission.messages.filter(is_read=False).exclude(sender=request.user).update(is_read=True)
    
    if request.method == 'POST':
        action = request.POST.get('action')
        
        # Xabar yuboriş
        if action == 'send_message':
            message_text = request.POST.get('message', '').strip()
            message_file = request.FILES.get('message_file')
            if message_text or message_file:
                from .models import HomeworkMessage
                HomeworkMessage.objects.create(
                    submission=submission,
                    sender=request.user,
                    message=message_text,
                    file=message_file
                )
                messages.success(request, "Xabar yuborildi!")
            return redirect('grade_homework', pk=pk)
        
        # Status özgartiriş
        elif action == 'set_reviewing':
            submission.status = 'reviewing'
            submission.save()
            messages.info(request, "Status: Körib çiqilmoqda")
            return redirect('grade_homework', pk=pk)
        
        # Qayta işlaşga yuboriş
        elif action == 'request_revision':
            revision_note = request.POST.get('revision_note', '')
            submission.status = 'revision'
            submission.feedback = revision_note
            submission.save()
            
            # Xabar qoldiriş
            from .models import HomeworkMessage
            HomeworkMessage.objects.create(
                submission=submission,
                sender=request.user,
                message=f"⚠️ Qayta işlaş kerak: {revision_note}"
            )
            
            # Notification
            Message.objects.create(
                title="Vazifani qayta işlang",
                content=f"'{submission.homework.lesson.title}' vazifangizni qayta işlaşingiz kerak.\n\nIzoh: {revision_note}",
                message_type='warning',
                recipient=submission.student
            )
            
            notify_user(
                submission.student.telegram_id,
                f"⚠️ <b>Vazifangiz qayta işlaş kerak!</b>\n\n"
                f"📚 {submission.homework.lesson.profession.name}\n"
                f"📖 {submission.homework.lesson.title}\n"
                f"💬 Izoh: {revision_note}\n\n"
                f"📝 Iltimos, tuzatib qayta yuboring!"
            )
            notify_group(
                f"🔄 <b>Uy vazifasi qaytarildi!</b>\n\n"
                f"👤 Öquvçi: {submission.student.first_name} {submission.student.last_name}\n"
                f"📚 Kurs: {submission.homework.lesson.profession.name}\n"
                f"📖 Dars: {submission.homework.lesson.title}\n"
                f"💬 Sabab: {revision_note}\n\n"
                f"👨‍🏫 Qaytardi: {request.user.first_name} {request.user.last_name}"
            )
            notify_admins(
                f"🔄 <b>Uy vazifasi qaytarildi!</b>\n\n"
                f"👤 {submission.student.first_name} {submission.student.last_name}\n"
                f"📚 {submission.homework.lesson.profession.name} → {submission.homework.lesson.title}\n"
                f"💬 {revision_note}\n"
                f"👨‍🏫 {request.user.first_name} {request.user.last_name}"
            )
            messages.warning(request, "Öquvçiga qayta işlaş sörovi yuborildi!")
            return redirect('grade_homework', pk=pk)
        
        # Qabul qiliş
        elif action == 'accept':
            submission.status = 'accepted'
            submission.save()
            messages.success(request, "Vazifa qabul qilindi!")
            return redirect('grade_homework', pk=pk)
        
        # Avtomatik baholaş (qabul + 5 ball)
        elif action == 'auto_grade':
            import random
            auto_feedbacks = [
                "Barakalla! Juda yaxşi işlagan ekansiz! 👏",
                "Juda ajoyib! Davom eting! 🌟",
                "Zör natija! Sizdan mamnunmiz! 💪",
                "Hammasi joyida! Yaxşi iş! ✨",
                "A'lo darajada bajarilgan! Rahmat! 🎯",
                "Ajoyib! Mehnatlaringiz uçun rahmat! 🏆",
                "Juda çiroyli işlangansiz! Davom eting! 🚀",
                "Yaxşi qilgansiz! Buni körib xursand böldim! 😊",
                "Super! Sizning sa'y-harakatlaringiz körinib turibdi! 💫",
                "Zör! Işingiz hammaga örnak böladi! 🎉",
            ]
            submission.status = 'graded'
            submission.grade = 5
            submission.feedback = random.choice(auto_feedbacks)
            submission.graded_by = request.user
            submission.graded_at = timezone.now()
            submission.save()
            
            if not submission.coin_awarded and submission.revision_count == 0:
                coin_amount = submission.calculate_coin_reward(5)
                submission.student.add_coins(coin_amount, f"Vazifa baholandi: {submission.homework.lesson.title}")
                submission.coin_awarded = True
                submission.coins_earned = coin_amount
                submission.save(update_fields=['coin_awarded', 'coins_earned'])

                ActivityLog.objects.create(
                    user=submission.student,
                    action_type='homework_graded',
                    description=f"Vazifa avtomatik baholandi: {submission.homework.lesson.title} - Baho: 5, +{coin_amount} coin"
                )
            
            from .notifications import on_homework_graded
            on_homework_graded(submission.student, submission, 5)
            _notify_homework_graded(request.user, submission, 5)
            
            messages.success(request, "Vazifa avtomatik baholandi: 5 ball!")
            return redirect('homework_submissions')
        
        # AI bilan baholaş
        elif action == 'ai_grade':
            if not _read_submission_content(submission):
                messages.error(request, "AI bu fayl formatini tekşira olmaydi (faqat matn va kod fayllari).")
                return redirect('grade_homework', pk=pk)
            # AI javobi 10–30 soniya kutiladi — sörov içida emas, fonda bajariladi.
            if cache.add(f'ai-grade-hw:{submission.pk}', 1, 600):
                ai_jobs.submit(_run_admin_ai_grade, submission.pk, request.user.pk)
            messages.info(request, "🤖 AI tekşiruvi boşlandi. Natija 1 daqiqa içida çiqadi — sahifani yangilang.")
            return redirect('homework_submissions')

        # Baholaş
        elif action == 'grade':
            grade = request.POST.get('grade')
            feedback = request.POST.get('feedback', '')
            feedback_file = request.FILES.get('feedback_file')
            
            if grade:
                submission.grade = min(int(grade), 10)
                submission.feedback = feedback
                if feedback_file:
                    submission.feedback_file = feedback_file
                submission.status = 'graded'
                submission.graded_by = request.user
                submission.graded_at = timezone.now()
                submission.save()
                
                # Coin beriş (faqat birinçi baholaşda: revision_count == 0)
                if not submission.coin_awarded and submission.revision_count == 0:
                    coin_amount = submission.calculate_coin_reward()

                    submission.student.add_coins(coin_amount, f"Vazifa baholandi: {submission.homework.lesson.title}")
                    submission.coin_awarded = True
                    submission.coins_earned = coin_amount
                    submission.save(update_fields=['coin_awarded', 'coins_earned'])

                    ActivityLog.objects.create(
                        user=submission.student,
                        action_type='homework_graded',
                        description=f"Vazifa baholandi: {submission.homework.lesson.title} - Baho: {submission.grade}, +{coin_amount} coin"
                    )

                # Notification
                from .notifications import on_homework_graded
                on_homework_graded(submission.student, submission, submission.grade)
                _notify_homework_graded(request.user, submission, submission.grade)
                notify_user(
                    submission.student.telegram_id,
                    f"✅ <b>Vazifangiz baholandi!</b>\n\n"
                    f"📚 {submission.homework.lesson.profession.name}\n"
                    f"📖 {submission.homework.lesson.title}\n"
                    f"🏆 Baho: <b>{submission.grade}/10</b>\n"
                    f"👨‍🏫 Baholadi: {request.user.first_name} {request.user.last_name}\n\n"
                    f"{'💬 <b>Izoh:</b>' + chr(10) + feedback if feedback else ''}"
                )

                messages.success(request, f"Vazifa baholandi: {submission.grade} ball!")
                return redirect('homework_submissions')
    
    return render(request, 'accounts/manage/grade_homework.html', {
        'submission': submission,
        'chat_messages': chat_messages,
        'revisions': revisions,
    })


@login_required
@ai_offload
def bulk_ai_grade(request):
    """Kurs böyiça barça kutilayotgan vazifalarni AI bilan ommaviy baholaş"""
    if not (request.user.is_admin or request.user.is_teacher):
        return JsonResponse({'error': 'Ruxsat yöq'}, status=403)

    if request.user.is_teacher and not request.user.is_admin:
        if not getattr(request.user, 'ai_access_enabled', False):
            return JsonResponse({'error': 'Sizda AI funksiyasidan foydalaniş ruxsati yöq'}, status=403)

    if request.method != 'POST':
        return JsonResponse({'error': 'POST sörovi kerak'}, status=405)

    profession_id = request.POST.get('profession_id', '').strip()
    if not profession_id:
        return JsonResponse({'error': 'Kurs tanlanmagan'}, status=400)

    if request.user.is_teacher and not request.user.is_admin:
        if not request.user.teaching_professions.filter(pk=profession_id).exists():
            return JsonResponse({'error': 'Sizda bu kursga kiriş huquqi yöq'}, status=403)

    submissions = HomeworkSubmission.objects.filter(
        homework__lesson__profession_id=profession_id,
        status__in=['pending', 'reviewing'],
    ).select_related('homework__lesson__profession', 'student')[:30]

    graded_count = 0
    revision_count = 0
    skipped_count = 0

    for submission in submissions:
        student_answer = _read_submission_content(submission)
        if not student_answer:
            skipped_count += 1
            continue

        ai_result = _get_ai_grade(submission.homework.description, student_answer)
        if not ai_result:
            skipped_count += 1
            continue

        submission.grade = ai_result['grade']
        submission.feedback = f"🤖 AI xulosasi:\n{ai_result['feedback']}"
        submission.status = ai_result['status']
        submission.graded_by = request.user
        submission.graded_at = timezone.now()
        submission.save()

        if ai_result['status'] == 'graded' and not submission.coin_awarded and submission.revision_count == 0:
            coin_amount = submission.calculate_coin_reward()
            submission.student.add_coins(coin_amount, f"Vazifa baholandi: {submission.homework.lesson.title}")
            submission.coin_awarded = True
            submission.coins_earned = coin_amount
            submission.save(update_fields=['coin_awarded', 'coins_earned'])
            ActivityLog.objects.create(
                user=submission.student,
                action_type='homework_graded',
                description=f"Vazifa AI (ommaviy) baholadi: {submission.homework.lesson.title} - Baho: {submission.grade}, +{coin_amount} coin"
            )

        if ai_result['status'] == 'graded':
            _notify_ai_graded(submission, ai_result)
            graded_count += 1
        else:
            Message.objects.create(
                title="Vazifani qayta işlang",
                content=f"'{submission.homework.lesson.title}' vazifangizni qayta işlaşingiz kerak.\n\nAI izohi: {ai_result['feedback']}",
                message_type='warning',
                recipient=submission.student
            )
            _notify_ai_revision(submission, ai_result)
            revision_count += 1

    return JsonResponse({
        'graded': graded_count,
        'revision': revision_count,
        'skipped': skipped_count,
        'total': graded_count + revision_count + skipped_count,
    })


def _teacher_reminder_templates(course_name, pending_count, oldest_hours):
    """Ustozga eslatma uçun tayyor rasmiy şablon matnlar.

    {ustoz} — yuborişda har bir öqituvçining ism-familiyasi bilan almaştiriladi.
    """
    return [
        {
            'key': 'polite',
            'label': "Muloyim eslatma",
            'text': (
                f"Hurmatli {{ustoz}}!\n\n"
                f"\"{course_name}\" kursida {pending_count} ta uy vazifasi "
                f"tekşirilişini kutmoqda. Öquvçilar Sizning bahoyingiz va "
                f"izohlaringizni intiqlik bilan kutişmoqda.\n\n"
                f"Iltimos, imkon qadar tezroq körib çiqişingizni söraymiz.\n\n"
                f"Hurmat bilan,\nIT Creative ma'muriyati"
            ),
        },
        {
            'key': 'daily',
            'label': "1 kundan oşgan vazifalar",
            'text': (
                f"Hurmatli {{ustoz}}!\n\n"
                f"\"{course_name}\" kursida 1 kundan buyon tekşirilmagan uy "
                f"vazifalari mavjud (jami {pending_count} ta, eng eskisi "
                f"{oldest_hours} soatdan buyon kutmoqda).\n\n"
                f"Öquv jarayonining sifatini ta'minlaş maqsadida vazifalarni "
                f"öz vaqtida tekşiriş muhim ekanligini eslatib ötamiz.\n\n"
                f"Hurmat bilan,\nIT Creative ma'muriyati"
            ),
        },
        {
            'key': 'warning',
            'label': "Rasmiy ogohlantiriş",
            'text': (
                f"Hurmatli {{ustoz}}!\n\n"
                f"\"{course_name}\" kursidagi uy vazifalari muntazam ravişda öz "
                f"vaqtida tekşirilmayotgani ma'lum böldi ({pending_count} ta "
                f"vazifa kutmoqda).\n\n"
                f"Uşbu holat mehnat majburiyatlariga rioya etmaslik sifatida "
                f"baholanişi va tegişli çoralar (jarima) qöllanilişi "
                f"mumkinligini rasman ma'lum qilamiz.\n\n"
                f"Iltimos, işga mas'uliyat bilan yondaşing.\n\n"
                f"Hurmat bilan,\nIT Creative ma'muriyati"
            ),
        },
    ]


@login_required
def notify_teacher_homework(request):
    """Admin: kurs ustoz(lar)iga tekşirilmagan vazifalar böyiça eslatma yuboriş.

    Içki çat (Message) + Telegram orqali yuboriladi. Faqat admin uçun.
    """
    if not request.user.is_admin:
        return JsonResponse({'error': "Faqat admin uçun ruxsat etilgan"}, status=403)
    if request.method != 'POST':
        return JsonResponse({'error': "POST sörovi kerak"}, status=405)

    profession_id = request.POST.get('profession_id', '').strip()
    text = request.POST.get('text', '').strip()
    if not profession_id or not text:
        return JsonResponse({'error': "Kurs va xabar matni kiritilişi şart"}, status=400)
    if len(text) > 3000:
        return JsonResponse({'error': "Xabar matni juda uzun (maks. 3000 belgi)"}, status=400)

    from django.conf import settings

    profession = get_object_or_404(Profession, pk=profession_id)
    teachers = [t for t in profession.teachers.all() if t.role == 'teacher']
    if not teachers:
        return JsonResponse({'error': "Bu kursga öqituvçi biriktirilmagan"}, status=400)

    site_url = getattr(settings, 'SITE_URL', 'https://itcreative.uz').rstrip('/')
    sent_chat = 0
    sent_telegram = 0
    for teacher in teachers:
        teacher_name = teacher.get_full_name() or teacher.username
        personal_text = text.replace('{ustoz}', teacher_name)

        # 1) Içki çat xabari
        try:
            Message.objects.create(
                title=f"📌 Eslatma: {profession.name} kursi vazifalari",
                content=personal_text,
                message_type='reminder',
                recipient=teacher,
                sender=request.user,
            )
            sent_chat += 1
        except Exception:
            pass

        # 2) Telegram xabari
        if teacher.telegram_id:
            tg_text = (
                f"📌 <b>MA'MURIYATDAN ESLATMA</b> ✨\n\n"
                f"{personal_text}\n\n"
                f"🔗 Vazifalarni tekşiriş: "
                f"{site_url}/homework-submissions/?profession={profession.pk}"
            )
            notify_user(teacher.telegram_id, tg_text)
            sent_telegram += 1

    # Audit jurnaliga yoziş
    try:
        from .models import AssistantActionLog
        AssistantActionLog.objects.create(
            user=request.user,
            action_code='homework.remind_teacher',
            target_type='profession',
            target_id=str(profession.pk),
            description=f"'{profession.name}' kursi ustoz(lar)iga vazifa eslatmasi yuborildi",
            ip_address=get_client_ip(request),
        )
    except Exception:
        pass

    return JsonResponse({
        'ok': True,
        'teachers': len(teachers),
        'chat': sent_chat,
        'telegram': sent_telegram,
    })


@login_required
def homework_chat(request, pk):
    """Öquvçi uçun vazifa çat sahifasi"""
    submission = get_object_or_404(HomeworkSubmission, pk=pk, student=request.user)
    chat_messages = submission.messages.all().order_by('created_at')
    revisions = submission.revisions.all()
    
    # Xabarlarni öqilgan deb belgilaş
    submission.messages.filter(is_read=False).exclude(sender=request.user).update(is_read=True)
    
    if request.method == 'POST':
        action = request.POST.get('action')
        
        # Xabar yuboriş
        if action == 'send_message':
            message_text = request.POST.get('message', '').strip()
            message_file = request.FILES.get('message_file')
            if message_text or message_file:
                from .models import HomeworkMessage
                HomeworkMessage.objects.create(
                    submission=submission,
                    sender=request.user,
                    message=message_text,
                    file=message_file
                )
                messages.success(request, "Xabar yuborildi!")
            return redirect('homework_chat', pk=pk)
        
        # Qayta topşiriş
        elif action == 'resubmit':
            new_file = request.FILES.get('new_file')
            new_text = request.POST.get('new_text_content', '').strip()
            note = request.POST.get('note', '')
            
            has_new_content = new_file or new_text
            
            if has_new_content:
                # Qayta topşiriş uçun belgilangan jarimani yeçiş
                resubmit_cost = (
                    HomeworkSubmission.RESUBMIT_COIN_PENALTY // 2
                    if request.user.is_premium
                    else HomeworkSubmission.RESUBMIT_COIN_PENALTY
                )
                request.user.remove_coins(resubmit_cost, f"Uyga vazifa qayta topşiriş: {submission.homework.lesson.title}")
                
                submission.archive_current_version(note=f"Versiya {submission.version}")
                
                # Yangi kontentni yuklaş
                if new_file:
                    submission.file = new_file
                    submission.text_content = None
                    submission.submission_type = 'file'
                elif new_text:
                    submission.text_content = new_text
                    submission.file = None
                    submission.submission_type = 'text'
                
                submission.version += 1
                submission.revision_count += 1
                submission.status = 'pending'
                submission.grade = None
                submission.feedback = ''
                submission.feedback_file = None
                submission.graded_by = None
                submission.graded_at = None
                submission.ai_feedback = None
                submission.ai_checked = False
                submission.submitted_at = timezone.now()
                submission.save()
                
                # Xabar qoldiriş
                from .models import HomeworkMessage
                msg_text = f"📎 Qayta işlangan versiya yuklandi (v{submission.version})."
                if note:
                    msg_text += f" {note}"
                HomeworkMessage.objects.create(
                    submission=submission,
                    sender=request.user,
                    message=msg_text
                )
                
                # Öqituvçi va adminlarga xabar yuboriş (Telegram + sayt)
                hw = submission.homework
                profession = hw.lesson.profession
                student = request.user
                
                # Sayt xabari — adminlarga
                notify_admins(
                    f"🔄 <b>Vazifa qayta topşirildi!</b>\n\n"
                    f"👤 {student.first_name} {student.last_name}\n"
                    f"📚 {profession.name} / {hw.lesson.title}\n"
                    f"📎 Versiya: v{submission.version}\n"
                    f"{'💬 Izoh: ' + note[:150] if note else ''}"
                )
                
                # Kurs öqituvçilariga xabar
                course_teachers = profession.teachers.filter(
                    telegram_id__isnull=False
                )
                for teacher in course_teachers:
                    notify_user(
                        teacher.telegram_id,
                        f"🔄 <b>Vazifa qayta topşirildi!</b>\n\n"
                        f"👤 Öquvçi: {student.first_name} {student.last_name}\n"
                        f"📚 Kurs: {profession.name}\n"
                        f"📖 Dars: {hw.lesson.title}\n"
                        f"📎 Versiya: v{submission.version}\n"
                        f"{'💬 Izoh: ' + note[:150] if note else ''}\n\n"
                        f"👨‍🏫 Tekşirişni kutmoqda!"
                    )
                
                # Saytda xabar yaratiş (öqituvçilar va adminlar körişi uçun)
                Message.objects.create(
                    recipient=None,
                    title=f"🔄 Vazifa qayta topşirildi: {hw.lesson.title}",
                    content=(
                        f"{student.first_name} {student.last_name} '{profession.name}' kursining "
                        f"'{hw.lesson.title}' darsiga vazifani qayta topşirdi (v{submission.version}). "
                        f"Tekşirişni kutmoqda."
                    ),
                    message_type='all'
                )
                
                messages.success(request, "Vazifa qayta topşirildi!")
            else:
                messages.error(request, "Fayl yoki matn kiriting!")
            return redirect('homework_chat', pk=pk)
    

    resubmit_cost = (
        HomeworkSubmission.RESUBMIT_COIN_PENALTY // 2
        if request.user.is_premium
        else HomeworkSubmission.RESUBMIT_COIN_PENALTY
    )
    return render(request, 'accounts/homework_chat.html', {
        'submission': submission,
        'chat_messages': chat_messages,
        'revisions': revisions,
        'resubmit_cost': resubmit_cost,
    })


@login_required
def my_test_results(request):
    """Öquvçining barça test natijalari (bugungi + barçasi, paginator bilan)"""
    from django.core.paginator import Paginator
    from datetime import date

    today = date.today()
    all_results = TestResult.objects.filter(
        student=request.user
    ).select_related('test__lesson__profession').order_by('-completed_at')

    today_results = all_results.filter(completed_at__date=today)

    stats = {
        'total': all_results.count(),
        'today': today_results.count(),
        'passed': all_results.filter(passed=True).count(),
        'failed': all_results.filter(passed=False).count(),
        'avg_score': all_results.aggregate(avg=Avg('score'))['avg'] or 0,
    }

    paginator = Paginator(all_results, 15)
    page = request.GET.get('page')
    results_page = paginator.get_page(page)

    return render(request, 'accounts/my_test_results.html', {
        'today_results': today_results,
        'results': results_page,
        'stats': stats,
    })


@login_required
def my_homework_list(request):
    """Öquvçining barça vazifalari"""
    submissions = HomeworkSubmission.objects.filter(student=request.user).select_related(
        'homework__lesson__profession'
    ).prefetch_related('messages').order_by('-submitted_at')
    
    # Statistika
    stats = {
        'total': submissions.count(),
        'pending': submissions.filter(status__in=['pending', 'reviewing']).count(),
        'revision': submissions.filter(status='revision').count(),
        'graded': submissions.filter(status='graded').count(),
        'avg_grade': submissions.filter(grade__isnull=False).aggregate(avg=Avg('grade'))['avg'] or 0,
    }
    
    return render(request, 'accounts/my_homeworks.html', {
        'submissions': submissions,
        'stats': stats,
    })


@login_required
def admin_test_analytics(request):
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')
    
    from django.db.models import Count, Avg, Q, F
    
    professions = Profession.objects.prefetch_related(
        'lessons__test__results', 'lessons__test__questions'
    ).all()
    
    # Umumiy statistika
    total_tests = Test.objects.count()
    total_attempts = TestResult.first_attempts().count()
    total_passed = TestResult.first_attempts().filter(passed=True).count()
    avg_score = TestResult.first_attempts().aggregate(avg=Avg('score'))['avg'] or 0
    pass_rate = round((total_passed / total_attempts * 100) if total_attempts > 0 else 0, 1)
    
    # Bugun işlangan testlar
    from datetime import date
    today = date.today()
    today_results = TestResult.objects.filter(
        completed_at__date=today
    ).select_related('test__lesson__profession', 'student').order_by('-completed_at')
    
    # Oxirgi natijalar (paginator bilan)
    from django.core.paginator import Paginator
    all_recent = TestResult.objects.select_related(
        'test__lesson__profession', 'student'
    ).order_by('-completed_at')
    paginator = Paginator(all_recent, 20)
    page = request.GET.get('page')
    recent_results = paginator.get_page(page)
    
    # Kurs tanlanganda
    selected_profession_id = request.GET.get('profession')
    selected_test_id = request.GET.get('test')
    selected_profession = None
    profession_tests = []
    selected_test = None
    test_results = []
    test_stats = {}
    
    if selected_profession_id:
        try:
            selected_profession = Profession.objects.get(pk=selected_profession_id)
            first_attempt_ids = TestResult.first_attempts().values_list('id', flat=True)
            profession_tests = Test.objects.filter(
                lesson__profession=selected_profession
            ).select_related('lesson').annotate(
                attempt_count=Count('results', filter=Q(results__id__in=first_attempt_ids)),
                pass_count=Count('results', filter=Q(results__passed=True, results__id__in=first_attempt_ids)),
                avg_score=Avg('results__score', filter=Q(results__id__in=first_attempt_ids))
            )
        except Profession.DoesNotExist:
            pass
    
    if selected_test_id:
        try:
            selected_test = Test.objects.select_related('lesson__profession').get(pk=selected_test_id)
            selected_profession = selected_test.lesson.profession
            test_results = TestResult.first_attempts().filter(
                test=selected_test
            ).select_related('student').order_by('-completed_at')
            
            total_t = test_results.count()
            passed_t = test_results.filter(passed=True).count()
            test_stats = {
                'total': total_t,
                'passed': passed_t,
                'failed': total_t - passed_t,
                'pass_rate': round((passed_t / total_t * 100) if total_t > 0 else 0, 1),
                'avg_score': round(test_results.aggregate(avg=Avg('score'))['avg'] or 0, 1),
                'best_score': test_results.order_by('-score').first(),
                'worst_score': test_results.order_by('score').first(),
            }
            
            # Savol böyiça tahlil
            question_analysis = []
            for question in selected_test.questions.prefetch_related('answers').all():
                total_answered = TestUserAnswer.objects.filter(
                    question=question,
                    result__test=selected_test
                ).count()
                correct_count = TestUserAnswer.objects.filter(
                    question=question,
                    result__test=selected_test,
                    is_correct=True
                ).count()
                wrong_count = total_answered - correct_count
                
                # Har bir javob variantining tanlangan soni
                answer_stats = []
                for answer in question.answers.all():
                    selected_count = TestUserAnswer.objects.filter(
                        question=question,
                        selected_answer=answer,
                        result__test=selected_test
                    ).count()
                    answer_stats.append({
                        'answer': answer,
                        'count': selected_count,
                        'percent': round((selected_count / total_answered * 100) if total_answered > 0 else 0),
                    })
                
                error_rate = round((wrong_count / total_answered * 100) if total_answered > 0 else 0)
                question_analysis.append({
                    'question': question,
                    'total': total_answered,
                    'correct': correct_count,
                    'wrong': wrong_count,
                    'error_rate': error_rate,
                    'answer_stats': answer_stats,
                })
            
            # Eng köp xato qilingan savollarni birinçi qöyiş
            question_analysis.sort(key=lambda x: x['error_rate'], reverse=True)
            
            # Also get profession_tests for this profession
            profession_tests = Test.objects.filter(
                lesson__profession=selected_profession
            ).select_related('lesson').annotate(
                attempt_count=Count('results', filter=Q(results__id__in=first_attempt_ids)),
                pass_count=Count('results', filter=Q(results__passed=True, results__id__in=first_attempt_ids)),
                avg_score=Avg('results__score', filter=Q(results__id__in=first_attempt_ids))
            )
        except Test.DoesNotExist:
            pass
    
    return render(request, 'accounts/admin/test_analytics.html', {
        'professions': professions,
        'total_tests': total_tests,
        'total_attempts': total_attempts,
        'total_passed': total_passed,
        'pass_rate': pass_rate,
        'avg_score': round(avg_score, 1),
        'today_results': today_results,
        'recent_results': recent_results,
        'selected_profession': selected_profession,
        'profession_tests': profession_tests,
        'selected_test': selected_test,
        'test_results': test_results,
        'test_stats': test_stats,
        'question_analysis': question_analysis if selected_test_id else [],
    })


@login_required
def admin_test_result_detail(request, pk):
    """Admin/Teacher: Öquvçining test natijasi - savol-javob tahlili"""
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')
    
    result = get_object_or_404(TestResult.objects.select_related(
        'test__lesson__profession', 'student'
    ), pk=pk)
    
    # Teacher faqat öz kurslaridagi natijalarni köra olsin
    if request.user.is_teacher and not request.user.is_admin:
        teaching_ids = request.user.teaching_professions.values_list('pk', flat=True)
        if result.test.lesson.profession_id not in teaching_ids:
            messages.error(request, "Sizda bu natijani köriş huquqi yöq!")
            return redirect('home')
    
    user_answers = result.user_answers.all().select_related(
        'question', 'selected_answer'
    ).order_by('question__order')
    
    # Har bir savol uçun töğri javobni ham oliş
    answer_details = []
    for ua in user_answers:
        correct_answer = ua.question.answers.filter(is_correct=True).first()
        answer_details.append({
            'question': ua.question,
            'selected': ua.selected_answer,
            'correct_answer': correct_answer,
            'is_correct': ua.is_correct,
        })
    
    # Determine back URL
    back_url = request.META.get('HTTP_REFERER', '')
    
    return render(request, 'accounts/admin/test_result_detail.html', {
        'result': result,
        'answer_details': answer_details,
        'back_url': back_url,
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
            
            # Öquvçiga çiroyli tabrik xabari yuboriş
            Message.objects.create(
                recipient=student,
                sender=request.user,
                title="🎓🎉 Tabriklaymiz! Sertifikat berildi!",
                content=(
                    f"🌟🌟🌟 TABRIKLAYMIZ! 🌟🌟🌟\n\n"
                    f"Hurmatli {student.full_name}!\n\n"
                    f"🎓 Sizga \"{profession.name}\" yönalişi böyiça SERTIFIKAT berildi!\n\n"
                    f"📋 Ma'lumotlar:\n"
                    f"   🆔 ID raqam: {student.id}\n"
                    f"   📚 Yönaliş: {profession.name}\n"
                    f"   🏅 Sertifikat ID: {cert.certificate_id}\n"
                    f"   📅 Berilgan sana: {cert.valid_from}\n"
                    f"   ✅ Holat: {cert.get_status_display()}\n\n"
                    f"🎊 Siz bu yönalişni muvaffaqiyatli tugatdingiz! Bu sizning mehnat va bilimingizning natijasi.\n\n"
                    f"🚀 Yangi kurslarda ham kutib qolamiz! IT Creative Academy sizga yanada köproq bilim va tajriba taqdim etişga tayyor.\n\n"
                    f"📜 Sertifikatingizni profil sahifangizdagi \"Sertifikatlarim\" bölimida körişingiz mumkin.\n\n"
                    f"💜 Hurmat bilan,\nIT Creative Academy jamoasi"
                ),
                message_type='achievement'
            )
            
            # Telegram orqali çiroyli tabrik xabari
            if student.telegram_id:
                telegram_text = (
                    f"🎓🎉🎊 <b>TABRIKLAYMIZ!</b> 🎊🎉🎓\n\n"
                    f"Hurmatli <b>{student.full_name}</b>!\n\n"
                    f"✨ Sizga <b>«{profession.name}»</b> yönalişi böyiça <b>SERTIFIKAT</b> berildi!\n\n"
                    f"📋 <b>Ma'lumotlar:</b>\n"
                    f"┌ 🆔 ID raqam: <b>{student.id}</b>\n"
                    f"├ 📚 Yönaliş: <b>{profession.name}</b>\n"
                    f"├ 🏅 Sertifikat: <b>{cert.certificate_id}</b>\n"
                    f"└ 📅 Sana: <b>{cert.valid_from}</b>\n\n"
                    f"🌟 Siz bu yönalişni muvaffaqiyatli tugatdingiz! Bu sizning mehnat va bilimingizning natijasi.\n\n"
                    f"🚀 <b>Yangi kurslarda ham kutib qolamiz!</b>\n"
                    f"IT Creative Academy sizga yanada köproq bilim va tajriba taqdim etişga tayyor.\n\n"
                    f"💜 <i>IT Creative Academy jamoasi</i>"
                )
                notify_user(student.telegram_id, telegram_text)
            
            # Adminlarga xabar
            notify_admins(
                f"🎓 <b>Sertifikat berildi!</b>\n\n"
                f"👤 Öquvçi: <b>{student.full_name}</b>\n"
                f"📚 Yönaliş: <b>{profession.name}</b>\n"
                f"🏅 Sertifikat: <b>{cert.certificate_id}</b>"
            )
            
            # Guruhga tabrik
            notify_group(
                f"🎓🎉 <b>TABRIKLAYMIZ!</b> 🎉🎓\n\n"
                f"🌟 <b>{student.full_name}</b> 🌟\n\n"
                f"<b>«{profession.name}»</b> yönalişini muvaffaqiyatli tugatdi va <b>SERTIFIKAT</b> oldi! 🏅\n\n"
                f"🎊 Tabriklaymiz va yangi kurslarda kutib qolamiz! 🚀\n\n"
                f"💜 <b>IT Creative Academy</b>"
            )
            
            messages.success(request, f"Sertifikat berildi! ID: {cert.certificate_id}")
            return redirect('admin_user_view', pk=pk)
    else:
        form = CertificateForm()
    
    return render(request, 'accounts/manage/issue_certificate.html', {
        'student': student,
        'professions': professions,
        'form': form,
    })


def verify_certificate(request):
    """Sertifikatni tekşiriş - login talab qilinmaydi"""
    certificate = None
    searched = False
    error = None
    student_results = None
    
    cert_id = request.GET.get('id', '').strip()
    if cert_id:
        searched = True
        try:
            certificate = Certificate.objects.select_related('student', 'profession', 'issued_by').get(certificate_id=cert_id)
            
            student = certificate.student
            profession = certificate.profession

            if student is None:
                # Qölda berilgan sertifikat — öquvçi natijalari mavjud emas
                return render(request, 'accounts/verify_certificate.html', {
                    'certificate': certificate,
                    'searched': searched,
                    'error': error,
                    'cert_id': cert_id,
                    'student_results': None,
                })

            # Kurs darslari
            lessons = Lesson.objects.filter(profession=profession)
            total_lessons = lessons.count()
            
            # Test natijalari
            test_results = TestResult.objects.filter(
                student=student,
                test__lesson__profession=profession
            ).select_related('test__lesson')
            tests_taken = test_results.count()
            tests_passed = test_results.filter(passed=True).count()
            avg_test_score = test_results.aggregate(avg=Avg('score'))['avg'] or 0
            
            # Uyga vazifa natijalari
            hw_submissions = HomeworkSubmission.objects.filter(
                student=student,
                homework__lesson__profession=profession,
                status__in=['accepted', 'graded']
            ).select_related('homework__lesson')
            hw_count = hw_submissions.count()
            avg_hw_grade = hw_submissions.aggregate(avg=Avg('grade'))['avg'] or 0
            
            # Speaking natijalari
            speaking_subs = SpeakingSubmission.objects.filter(
                student=student,
                speaking_task__lesson__profession=profession,
                status='graded'
            ).select_related('speaking_task__lesson')
            speaking_count = speaking_subs.count()
            avg_speaking_grade = speaking_subs.aggregate(avg=Avg('grade'))['avg'] or 0
            
            # Umumiy örtaça ball hisoblaş
            all_scores = []
            if tests_taken > 0:
                all_scores.append(avg_test_score)
            if hw_count > 0:
                all_scores.append(avg_hw_grade * 10)  # 10-ballik -> 100-ballik
            if speaking_count > 0:
                all_scores.append(avg_speaking_grade * 10)
            overall_avg = sum(all_scores) / len(all_scores) if all_scores else 0
            
            student_results = {
                'total_lessons': total_lessons,
                'tests_taken': tests_taken,
                'tests_passed': tests_passed,
                'avg_test_score': round(avg_test_score, 1),
                'hw_count': hw_count,
                'avg_hw_grade': round(avg_hw_grade, 1),
                'speaking_count': speaking_count,
                'avg_speaking_grade': round(avg_speaking_grade, 1),
                'overall_avg': round(overall_avg, 1),
                'test_results': test_results[:10],
            }
            
        except Certificate.DoesNotExist:
            error = "Bu ID raqamga mos sertifikat topilmadi."
    
    return render(request, 'accounts/verify_certificate.html', {
        'certificate': certificate,
        'searched': searched,
        'error': error,
        'cert_id': cert_id,
        'student_results': student_results,
    })


# Admin Panel Views
@login_required
def admin_dashboard(request):
    if not request.user.is_admin:
        messages.error(request, "Sizda admin paneliga kiriş huquqi yöq!")
        return redirect('home')
    
    dash_stats = get_performance_cache().get('admin_dash_stats')
    if dash_stats is None:
        dash_stats = {
            'total_users': CustomUser.objects.count(),
            'total_teachers': CustomUser.objects.filter(role='teacher').count(),
            'total_students': CustomUser.objects.filter(role='student').count(),
            'total_professions': Profession.objects.count(),
            'blocked_users': CustomUser.objects.filter(is_blocked=True).count(),
            'total_lessons': Lesson.objects.count(),
            'total_videos': VideoLesson.objects.count(),
            'total_tests': Test.objects.count(),
            'total_homeworks': Homework.objects.count(),
            'total_enrollments': CourseEnrollment.objects.count(),
            'total_certificates': Certificate.objects.count(),
            'total_coins': CustomUser.objects.aggregate(total=Sum('coins'))['total'] or 0,
        }
        get_performance_cache().set('admin_dash_stats', dash_stats, 120)
    total_users = dash_stats['total_users']
    total_teachers = dash_stats['total_teachers']
    total_students = dash_stats['total_students']
    total_professions = dash_stats['total_professions']
    blocked_users = dash_stats['blocked_users']
    online_users = CustomUser.objects.filter(
        last_activity__gte=timezone.now() - timedelta(seconds=300)
    ).count()
    total_lessons = dash_stats['total_lessons']
    total_videos = dash_stats['total_videos']
    total_tests = dash_stats['total_tests']
    total_homeworks = dash_stats['total_homeworks']
    total_enrollments = dash_stats['total_enrollments']
    total_certificates = dash_stats['total_certificates']
    total_coins = dash_stats['total_coins']
    
    # Test natijalari (cached)
    test_cache = get_performance_cache().get('admin_test_stats')
    if test_cache is None:
        fa = TestResult.first_attempts()
        test_cache = {
            'total': fa.count(),
            'passed': fa.filter(passed=True).count(),
            'avg': fa.aggregate(avg=Avg('score'))['avg'] or 0,
        }
        get_performance_cache().set('admin_test_stats', test_cache, 300)
    total_test_results = test_cache['total']
    passed_tests = test_cache['passed']
    avg_test_score = test_cache['avg']
    
    # Uyga vazifalar
    pending_homeworks = HomeworkSubmission.objects.filter(status='pending').count()
    graded_homeworks = HomeworkSubmission.objects.filter(status='graded').count()
    
    # Mahsulot statistikasi
    from coin.models import Product, ProductPurchase
    total_products = Product.objects.count()
    active_products = Product.objects.filter(is_active=True).count()
    total_purchases = ProductPurchase.objects.count()
    total_coins_spent = ProductPurchase.objects.aggregate(total=Sum('coins_spent'))['total'] or 0
    
    # Ota-onalar statistikasi
    total_parents = Parent.objects.count()
    active_parents = Parent.objects.filter(is_active=True).count()
    
    # Oxirgi harakatlar
    recent_activities = ActivityLog.objects.select_related('user').order_by('-created_at')[:10]
    
    profession_stats = Profession.objects.annotate(
        student_count=Count('enrollments', filter=Q(enrollments__user__role='student')),
        teacher_count=Count('students', filter=Q(students__role='teacher'))
    )
    
    recent_users = CustomUser.objects.filter(last_activity__isnull=False).order_by('-last_activity')[:10]
    
    # Oxirgi 7 kun statistikasi
    today = timezone.now().date()
    week_ago = today - timedelta(days=6)
    
    user_counts = dict(
        CustomUser.objects.filter(date_joined__date__gte=week_ago)
        .values_list('date_joined__date')
        .annotate(c=Count('id'))
        .values_list('date_joined__date', 'c')
    )
    enrollment_counts = dict(
        CourseEnrollment.objects.filter(enrolled_at__date__gte=week_ago)
        .values_list('enrolled_at__date')
        .annotate(c=Count('id'))
        .values_list('enrolled_at__date', 'c')
    )
    
    daily_stats = []
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        daily_stats.append({
            'date': day,
            'users': user_counts.get(day, 0),
            'enrollments': enrollment_counts.get(day, 0),
        })
    
    # Texnik xizmat rejimi
    maintenance = MaintenanceMode.get_instance()

    # ============ MOLIYA VA ÖSIŞ KÖRSATKIÇLARI (aniq hisob) ============
    _now = timezone.now()
    _cur_m, _cur_y = _now.month, _now.year
    if _cur_m == 1:
        _prev_m, _prev_y = 12, _cur_y - 1
    else:
        _prev_m, _prev_y = _cur_m - 1, _cur_y

    finance = get_performance_cache().get('admin_finance_stats')
    if finance is None:
        _paid_agg = PaymentRecord.objects.filter(
            is_paid=True, month=_cur_m, year=_cur_y
        ).aggregate(s=Sum('paid_amount'), c=Count('id'))
        _prev_agg = PaymentRecord.objects.filter(
            is_paid=True, month=_prev_m, year=_prev_y
        ).aggregate(s=Sum('paid_amount'))
        # Qarzdorlik oy boşidan hisoblanadi: joriy oy uçun tölangan yozuvi
        # yöq har bir faol öquvçi qarzdor (tölanmagan yozuv yaratilişini
        # kutmaymiz). Şartnoma, kursni tugatganlar va oy oxirigaça oldindan
        # tölaganlar (next_payment_date oy oxiridan keyin) hisobga olinmaydi.
        from calendar import monthrange
        _month_end = date(_cur_y, _cur_m, monthrange(_cur_y, _cur_m)[1])
        _debtors = CustomUser.objects.filter(
            role='student', is_active=True, is_finished=False
        ).exclude(
            student_balance__is_contract=True
        ).exclude(
            student_balance__next_payment_date__gt=_month_end
        ).exclude(
            pk__in=PaymentRecord.objects.filter(
                is_paid=True, month=_cur_m, year=_cur_y
            ).values('user_id')
        ).values_list('id', 'payment_status__monthly_fee')
        _open_debts = {
            uid: max(0, int(amt or 0) - int(paid or 0))
            for uid, amt, paid in PaymentRecord.objects.filter(
                is_paid=False, month=_cur_m, year=_cur_y
            ).values_list('user_id', 'amount', 'paid_amount')
        }
        _unpaid_sum = 0
        _unpaid_cnt = 0
        for _uid, _fee in _debtors:
            _debt = _open_debts.get(_uid, 200000 if _fee is None else int(_fee))
            if _debt > 0:
                _unpaid_sum += _debt
                _unpaid_cnt += 1
        _unpaid_agg = {'s': _unpaid_sum, 'c': _unpaid_cnt}
        _cur_sum = int(_paid_agg['s'] or 0)
        _prev_sum = int(_prev_agg['s'] or 0)
        if _prev_sum:
            _rev_change = round((_cur_sum - _prev_sum) / _prev_sum * 100)
        else:
            _rev_change = 100 if _cur_sum else 0
        finance = {
            'month_revenue': _cur_sum,
            'month_paid_count': _paid_agg['c'] or 0,
            'prev_revenue': _prev_sum,
            'revenue_change': _rev_change,
            'month_unpaid': int(_unpaid_agg['s'] or 0),
            'month_unpaid_count': _unpaid_agg['c'] or 0,
        }
        get_performance_cache().set('admin_finance_stats', finance, 300)

    growth = get_performance_cache().get('admin_growth_stats')
    if growth is None:
        _month_start = _now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        _new_this_month = CustomUser.objects.filter(date_joined__gte=_month_start).count()
        _prev_start = (_month_start - timedelta(days=1)).replace(day=1)
        _new_prev_month = CustomUser.objects.filter(
            date_joined__gte=_prev_start, date_joined__lt=_month_start
        ).count()
        if _new_prev_month:
            _user_change = round((_new_this_month - _new_prev_month) / _new_prev_month * 100)
        else:
            _user_change = 100 if _new_this_month else 0
        growth = {
            'new_this_month': _new_this_month,
            'new_prev_month': _new_prev_month,
            'user_change': _user_change,
        }
        get_performance_cache().set('admin_growth_stats', growth, 300)

    # Kurslar samaradorligi jadvali (top 10, öquvçilar soni böyiça)
    course_perf = get_performance_cache().get('admin_course_perf')
    if course_perf is None:
        _perf_qs = Profession.objects.annotate(
            student_total=Count(
                'enrollments', filter=Q(enrollments__user__role='student'), distinct=True
            ),
            lesson_total=Count('lessons', distinct=True),
        ).order_by('-student_total')[:10]
        _perf_ids = [p.pk for p in _perf_qs]
        _pending_map = dict(
            HomeworkSubmission.objects.filter(
                status='pending', homework__lesson__profession_id__in=_perf_ids
            ).values_list('homework__lesson__profession_id').annotate(c=Count('id'))
            .values_list('homework__lesson__profession_id', 'c')
        )
        _score_map = dict(
            TestResult.objects.filter(
                test__lesson__profession_id__in=_perf_ids
            ).values_list('test__lesson__profession_id').annotate(a=Avg('score'))
            .values_list('test__lesson__profession_id', 'a')
        )
        course_perf = []
        for p in _perf_qs:
            course_perf.append({
                'name': p.name,
                'students': p.student_total,
                'lessons': p.lesson_total,
                'pending': _pending_map.get(p.pk, 0),
                'avg_score': round(_score_map.get(p.pk) or 0, 1),
            })
        get_performance_cache().set('admin_course_perf', course_perf, 300)

    # Aurora dashboard grafiklari — yangi sörov yöq, yuqoridagi ma'lumotlardan
    _wd = ['Du', 'Se', 'Ço', 'Pa', 'Ju', 'Şa', 'Ya']
    trend_chart = {
        'labels': [_wd[d['date'].weekday()] + ' ' + d['date'].strftime('%d') for d in daily_stats],
        'series': [
            {'name': 'Yangi foydalanuvçilar', 'values': [d['users'] for d in daily_stats]},
            {'name': 'Kursga yozilişlar', 'values': [d['enrollments'] for d in daily_stats]},
        ],
        'aria': 'Oxirgi 7 kun: yangi foydalanuvçilar va kursga yozilişlar',
    }
    _donut_colors = ['var(--au-c1)', 'var(--au-c2)', 'var(--au-c3)', 'var(--au-c4)']
    _top = course_perf[:4]
    _rest = sum(c['students'] for c in course_perf[4:])
    course_donut = {
        'segs': [{'v': c['students'], 'c': _donut_colors[i], 'label': c['name']} for i, c in enumerate(_top)]
        + ([{'v': _rest, 'c': 'var(--au-text-3)', 'label': 'Qolgan TOP-10 kurslar'}] if _rest else []),
        'value': sum(c['students'] for c in course_perf),
        'label': 'yoziliş · TOP-10',
    }
    test_donut = {
        'segs': [{'v': passed_tests, 'c': 'var(--au-ok)', 'label': 'Ötgan'},
                 {'v': max(0, total_test_results - passed_tests), 'c': 'var(--au-bad)', 'label': 'Ötmagan'}],
        'value': round(passed_tests / total_test_results * 100) if total_test_results else 0,
        'suffix': '%', 'label': 'ötiş darajasi', 'size': 140, 'stroke': 14,
    }

    context = {
        'trend_chart': trend_chart,
        'course_donut': course_donut,
        'test_donut': test_donut,
        'users_spark': [d['users'] for d in daily_stats],
        'maintenance': maintenance,
        'finance': finance,
        'growth': growth,
        'course_perf': course_perf,
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
        'recent_activities': recent_activities,
        'total_parents': total_parents,
        'active_parents': active_parents,
    }
    return render(request, 'accounts/admin/dashboard.html', context)


@login_required
def admin_export_report(request):
    """Admin: tizim hisobotini Excel (.xlsx) körinişida yuklab oliş.

    4 varaq: Umumiy, Kurslar, Tölovlar (joriy yil), Öqituvçilar.
    """
    if not request.user.is_admin:
        return redirect('home')

    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = openpyxl.Workbook()
    header_fill = PatternFill(start_color='4F46E5', end_color='4F46E5', fill_type='solid')
    header_font = Font(bold=True, color='FFFFFF')

    def _style_header(ws):
        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal='center')

    def _fit_columns(ws):
        for col in ws.columns:
            width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
            ws.column_dimensions[col[0].column_letter].width = min(42, max(10, width + 2))

    # ---- 1) Umumiy ----
    ws1 = wb.active
    ws1.title = "Umumiy"
    ws1.append(["Körsatkiç", 'Qiymat'])
    _style_header(ws1)
    general_rows = [
        ("Jami foydalanuvçilar", CustomUser.objects.count()),
        ("Öquvçilar", CustomUser.objects.filter(role='student').count()),
        ("Öqituvçilar", CustomUser.objects.filter(role='teacher').count()),
        ("Yordamçi adminlar", CustomUser.objects.filter(role='assistant_admin').count()),
        ("Bloklangan foydalanuvçilar", CustomUser.objects.filter(is_blocked=True).count()),
        ("Kurslar", Profession.objects.count()),
        ("Darslar", Lesson.objects.count()),
        ("Video darslar", VideoLesson.objects.count()),
        ("Testlar", Test.objects.count()),
        ("Kursga yozilişlar", CourseEnrollment.objects.count()),
        ("Sertifikatlar", Certificate.objects.count()),
        ("Tekşirilmagan vazifalar", HomeworkSubmission.objects.filter(status='pending').count()),
    ]
    for row in general_rows:
        ws1.append(list(row))
    _fit_columns(ws1)

    # ---- 2) Kurslar ----
    ws2 = wb.create_sheet("Kurslar")
    ws2.append(['#', 'Kurs', "Öquvçilar", 'Darslar', 'Tekşirilmagan vazifa', "Örtaça test bali (%)", "Öqituvçilar"])
    _style_header(ws2)
    profs = Profession.objects.annotate(
        student_total=Count('enrollments', filter=Q(enrollments__user__role='student'), distinct=True),
        lesson_total=Count('lessons', distinct=True),
    ).prefetch_related('teachers').order_by('-student_total')
    prof_ids = [p.pk for p in profs]
    pending_map = dict(
        HomeworkSubmission.objects.filter(
            status='pending', homework__lesson__profession_id__in=prof_ids
        ).values_list('homework__lesson__profession_id').annotate(c=Count('id'))
        .values_list('homework__lesson__profession_id', 'c')
    )
    score_map = dict(
        TestResult.objects.filter(
            test__lesson__profession_id__in=prof_ids
        ).values_list('test__lesson__profession_id').annotate(a=Avg('score'))
        .values_list('test__lesson__profession_id', 'a')
    )
    for i, p in enumerate(profs, 1):
        teacher_names = ", ".join(
            t.get_full_name() or t.username for t in p.teachers.all()
        )
        ws2.append([
            i, p.name, p.student_total, p.lesson_total,
            pending_map.get(p.pk, 0), round(score_map.get(p.pk) or 0, 1),
            teacher_names,
        ])
    _fit_columns(ws2)

    # ---- 3) Tölovlar (joriy yil, oylar böyiça) ----
    ws3 = wb.create_sheet("Tölovlar")
    ws3.append(['Oy', "Tölangan (söm)", "Tölovlar soni", "Tölanmagan (söm)", "Qarzdorlar soni"])
    _style_header(ws3)
    year = timezone.now().year
    month_names = ['Yanvar', 'Fevral', 'Mart', 'Aprel', 'May', 'Iyun',
                   'Iyul', 'Avgust', 'Sentabr', 'Oktabr', 'Noyabr', 'Dekabr']
    paid_rows = {
        r['month']: r for r in PaymentRecord.objects.filter(is_paid=True, year=year)
        .values('month').annotate(s=Sum('paid_amount'), c=Count('id'))
    }
    unpaid_rows = {
        r['month']: r for r in PaymentRecord.objects.filter(is_paid=False, year=year)
        .values('month').annotate(s=Sum('amount'), c=Count('id'))
    }
    for m in range(1, 13):
        p = paid_rows.get(m, {})
        u = unpaid_rows.get(m, {})
        ws3.append([
            month_names[m - 1],
            int(p.get('s') or 0), p.get('c') or 0,
            int(u.get('s') or 0), u.get('c') or 0,
        ])
    ws3.append([
        f"JAMI ({year})",
        sum(int(r.get('s') or 0) for r in paid_rows.values()),
        sum(r.get('c') or 0 for r in paid_rows.values()),
        sum(int(r.get('s') or 0) for r in unpaid_rows.values()),
        sum(r.get('c') or 0 for r in unpaid_rows.values()),
    ])
    for cell in ws3[ws3.max_row]:
        cell.font = Font(bold=True)
    _fit_columns(ws3)

    # ---- 4) Öqituvçilar ----
    ws4 = wb.create_sheet("Öqituvçilar")
    ws4.append(['#', 'Ism-familiya', 'Telefon', 'Kurslari', "Öquvçilari",
                'Tekşirilmagan vazifa', 'Baholagan vazifalari'])
    _style_header(ws4)
    teachers = CustomUser.objects.filter(role='teacher').prefetch_related('teaching_professions')
    for i, t in enumerate(teachers, 1):
        t_prof_ids = [p.pk for p in t.teaching_professions.all()]
        t_students = CourseEnrollment.objects.filter(
            profession_id__in=t_prof_ids, user__role='student'
        ).values('user').distinct().count() if t_prof_ids else 0
        t_pending = HomeworkSubmission.objects.filter(
            homework__lesson__profession_id__in=t_prof_ids, status='pending'
        ).count() if t_prof_ids else 0
        t_graded = HomeworkSubmission.objects.filter(
            graded_by=t
        ).count()
        ws4.append([
            i, t.get_full_name() or t.username, t.phone,
            ", ".join(p.name for p in t.teaching_professions.all()),
            t_students, t_pending, t_graded,
        ])
    _fit_columns(ws4)

    stamp = timezone.localdate().strftime('%Y%m%d')
    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="tizim_hisoboti_{stamp}.xlsx"'
    wb.save(response)
    return response


@login_required
def toggle_maintenance(request):
    """Texnik xizmat rejimini yoqish/öchirish"""
    if not request.user.is_admin:
        return redirect('home')
    
    maintenance = MaintenanceMode.get_instance()
    maintenance.is_active = not maintenance.is_active
    maintenance.activated_by = request.user
    
    if request.method == 'POST':
        custom_message = request.POST.get('maintenance_message', '').strip()
        if custom_message:
            maintenance.message = custom_message
    
    maintenance.save()
    
    if maintenance.is_active:
        notify_group(
            f"🔧 <b>Texnik xizmat rejimi yoqildi!</b>\n\n"
            f"⚠️ Sayt vaqtinça işlamaydi.\n"
            f"📝 {maintenance.message}\n\n"
            f"👤 Admin: <b>{request.user.full_name}</b>"
        )
        for u in CustomUser.objects.filter(telegram_id__isnull=False, is_blocked=False):
            notify_user(
                u.telegram_id,
                f"🔧 <b>Texnik xizmat rejimi yoqildi!</b>\n\n"
                f"⚠️ Sayt vaqtinça işlamaydi.\n"
                f"📝 {maintenance.message}\n\n"
                f"⏳ Iltimos, keyinroq urinib köring."
            )
    else:
        notify_group(
            f"✅ <b>Sayt yana işlayapti!</b>\n\n"
            f"🎉 Texnik xizmat rejimi öçirildi.\n"
            f"🚀 Saytga kirişingiz mumkin!\n\n"
            f"👤 Admin: <b>{request.user.full_name}</b>"
        )
        for u in CustomUser.objects.filter(telegram_id__isnull=False, is_blocked=False):
            notify_user(
                u.telegram_id,
                f"✅ <b>Sayt yana işlayapti!</b>\n\n"
                f"🎉 Texnik işlar tugadi.\n"
                f"🚀 Saytga kirişingiz mumkin!"
            )
    
    status = "yoqildi" if maintenance.is_active else "öçirildi"
    messages.success(request, f"Texnik xizmat rejimi {status}!")
    return redirect('admin_dashboard')


@login_required
def admin_professions(request):
    if not request.user.is_admin:
        return redirect('home')
    
    professions = Profession.objects.prefetch_related('enrollments__user', 'teachers').annotate(
        student_count=Count('enrollments', filter=Q(enrollments__user__role='student')),
        teacher_count=Count('students', filter=Q(students__role='teacher'))
    )
    all_students = CustomUser.objects.filter(
        role__in=('student', 'assistant_admin')
    ).order_by('first_name', 'last_name')
    return render(request, 'accounts/admin/professions.html', {'professions': professions, 'all_students': all_students})


def _enroll_all_students_mandatory(profession):
    """Majburiy kursga barça öquvçilarni avtomatik yoziş"""
    students = CustomUser.objects.filter(role='student')
    created_count = 0
    for student in students:
        _, created = CourseEnrollment.objects.get_or_create(user=student, profession=profession)
        if created:
            created_count += 1
            if student.telegram_id:
                notify_user(
                    student.telegram_id,
                    f"📌 <b>Majburiy kurs tayinlandi!</b>\n\n"
                    f"📖 <b>{profession.name}</b>\n\n"
                    f"📚 Siz uşbu kursga avtomatik yozildingiz. Öqişni boşlang!"
                )
    if created_count:
        notify_admins(
            f"📌 <b>Majburiy kurs belgilandi!</b>\n\n"
            f"📖 {profession.name}\n"
            f"👥 {created_count} ta öquvçi avtomatik yozildi"
        )


@login_required
def admin_profession_add(request):
    if not request.user.is_admin:
        return redirect('home')
    
    if request.method == 'POST':
        form = ProfessionForm(request.POST, request.FILES)
        if form.is_valid():
            profession = form.save()
            if profession.is_mandatory:
                _enroll_all_students_mandatory(profession)
            messages.success(request, "Kasb muvaffaqiyatli qöşildi!")
            return redirect('admin_professions')
    else:
        form = ProfessionForm()
    
    return render(request, 'accounts/admin/profession_form.html', {'form': form, 'title': "Kasb qöşiş"})


@login_required
def admin_profession_edit(request, pk):
    if not request.user.is_admin:
        return redirect('home')
    
    profession = get_object_or_404(Profession, pk=pk)
    was_mandatory = profession.is_mandatory
    
    if request.method == 'POST':
        form = ProfessionForm(request.POST, request.FILES, instance=profession)
        if form.is_valid():
            profession = form.save()
            if profession.is_mandatory and not was_mandatory:
                _enroll_all_students_mandatory(profession)
            messages.success(request, "Kasb muvaffaqiyatli tahrirlandi!")
            return redirect('admin_professions')
    else:
        form = ProfessionForm(instance=profession)
    
    return render(request, 'accounts/admin/profession_form.html', {'form': form, 'title': "Kasbni tahrirlaş", 'profession': profession})


@login_required
def admin_profession_delete(request, pk):
    if not request.user.is_admin:
        return redirect('home')
    
    profession = get_object_or_404(Profession, pk=pk)
    
    if request.method == 'POST':
        profession.delete()
        messages.success(request, "Kasb muvaffaqiyatli öçirildi!")
        return redirect('admin_professions')
    
    return render(request, 'accounts/admin/profession_delete.html', {'profession': profession})


@login_required
def admin_profession_toggle_block(request, pk):
    """Kasbni bloklash/blokdan çiqariş"""
    if not request.user.is_admin:
        return redirect('home')
    
    profession = get_object_or_404(Profession, pk=pk)
    
    if request.method == 'POST':
        profession.is_blocked = not profession.is_blocked
        if profession.is_blocked:
            profession.blocked_reason = request.POST.get('blocked_reason', '').strip() or "Vaqtinça töxtatilgan"
            messages.warning(request, f"'{profession.name}' kursi bloklandi!")
        else:
            profession.blocked_reason = None
            messages.success(request, f"'{profession.name}' kursi blokdan çiqarildi!")
        profession.save()
    
    return redirect('admin_professions')


@login_required
def admin_profession_toggle_certificate(request, pk):
    """Kursga sertifikat joriy qiliş / olib taşlaş"""
    if not request.user.is_admin:
        return redirect('home')

    profession = get_object_or_404(Profession, pk=pk)

    if request.method == 'POST':
        profession.has_certificate = not profession.has_certificate
        profession.save(update_fields=['has_certificate'])
        if profession.has_certificate:
            messages.success(request, f"'{profession.name}' uçun sertifikat joriy qilindi! 🎓 Endi öquvçilar sertifikat sörovi yubora oladi.")
        else:
            messages.warning(request, f"'{profession.name}' uçun sertifikat olib taşlandi. Endi öquvçilar sertifikat sörovi yubora olmaydi.")

    return redirect('admin_professions')


@login_required
def admin_profession_toggle_daily_limit(request, pk):
    """Kunlik bölim çeklovini yoqish/öchirish"""
    if not request.user.is_admin:
        return redirect('home')
    
    profession = get_object_or_404(Profession, pk=pk)
    
    if request.method == 'POST':
        profession.daily_section_limit = not profession.daily_section_limit
        profession.save()
        if profession.daily_section_limit:
            messages.success(request, f"'{profession.name}' uçun kunlik bölim çeklovi yoqildi! 🔒")
        else:
            messages.success(request, f"'{profession.name}' uçun kunlik bölim çeklovi öçirildi! 🔓")
    
    return redirect('admin_professions')


@login_required
def admin_user_daily_limit(request, user_pk, profession_pk):
    """Öquvçining şaxsiy kunlik çeklov holatini özgartiriş.

    mode: inherit (kurs sozlamasi), on (majburan yoqiş), off (majburan öçiriş)
    """
    if not request.user.is_admin:
        return redirect('home')

    user = get_object_or_404(CustomUser, pk=user_pk)
    profession = get_object_or_404(Profession, pk=profession_pk)
    enrollment = CourseEnrollment.objects.filter(
        user=user, profession=profession
    ).first()

    if request.method == 'POST' and enrollment:
        mode = request.POST.get('mode', 'inherit')
        if mode == 'on':
            enrollment.daily_limit_override = True
            label = "yoqildi 🔒"
        elif mode == 'off':
            enrollment.daily_limit_override = False
            label = "öçirildi 🔓"
        else:
            enrollment.daily_limit_override = None
            label = "kurs sozlamasiga qaytarildi"
        enrollment.save(update_fields=['daily_limit_override'])

        messages.success(
            request,
            f"{user.full_name} uçun '{profession.name}' kursida kunlik çeklov {label}.",
        )

    return redirect('admin_user_view', pk=user_pk)


@login_required
def admin_remove_student_from_course(request, profession_pk, user_pk):
    if not request.user.is_admin:
        return redirect('home')
    
    profession = get_object_or_404(Profession, pk=profession_pk)
    student = get_object_or_404(CustomUser, pk=user_pk)
    
    if request.method == 'POST':
        enrollment = CourseEnrollment.objects.filter(user=student, profession=profession).first()
        if enrollment:
            enrollment.delete()
            if student.profession == profession:
                student.profession = None
                student.save(update_fields=['profession'])
            messages.success(request, f"{student.full_name} '{profession.name}' kursidan çiqarildi!")
        else:
            messages.warning(request, f"{student.full_name} bu kursga yozilmagan!")
    
    return redirect('admin_professions')


@login_required
def admin_add_student_to_course(request, profession_pk):
    """Admin: Öquvçini kursga qöşiş"""
    if not request.user.is_admin:
        return redirect('home')
    
    profession = get_object_or_404(Profession, pk=profession_pk)
    
    if request.method == 'POST':
        user_pk = request.POST.get('user_pk')
        if user_pk:
            student = get_object_or_404(
                CustomUser, pk=user_pk, role__in=('student', 'assistant_admin')
            )
            if CourseEnrollment.objects.filter(user=student, profession=profession).exists():
                messages.info(request, f"{student.full_name} allaqaçon '{profession.name}' kursiga yozilgan!")
            else:
                CourseEnrollment.objects.create(user=student, profession=profession)
                # Eski sörovlarni tozalaş (qayta yozilişga tösqinlik qilmasligi uçun)
                CourseEnrollmentRequest.objects.filter(user=student, profession=profession).delete()
                CourseUnenrollRequest.objects.filter(user=student, profession=profession).delete()
                # Qayta yoziliş bölsa — eski progressni tozalaş
                _reset_course_progress(student, profession)
                messages.success(request, f"{student.full_name} '{profession.name}' kursiga muvaffaqiyatli qöşildi!")
    
    return redirect('admin_professions')


@login_required
def admin_users(request):
    if not request.user.is_admin:
        return redirect('home')
    
    search_query = request.GET.get('q', '').strip()
    role_filter = request.GET.get('role', '')
    profession_filter = request.GET.get('profession', '')
    status_filter = request.GET.get('status', '')
    group_filter = request.GET.get('group', '')
    payment_filter = request.GET.get('payment', '')
    dob_filter = request.GET.get('dob', '')
    sort_by = request.GET.get('sort', '')

    from django.db.models import Exists, OuterRef, Prefetch
    from .models import StudyGroup, StudentBalance

    users = CustomUser.objects.only(
        'id', 'username', 'first_name', 'last_name', 'phone', 'photo',
        'role', 'is_blocked', 'is_finished', 'is_premium', 'last_activity', 'date_joined',
        'is_superuser', 'student_id', 'birth_date', 'gender',
    ).prefetch_related(
        # Röyxatda har bir öquvçining guruh(lar)ini körsatiş uçun
        Prefetch('study_groups', queryset=StudyGroup.objects.only('id', 'name', 'color'))
    ).annotate(
        # Şartnoma rejimidagi öquvçilarni belgilaş uçun
        has_contract=Exists(
            StudentBalance.objects.filter(user_id=OuterRef('pk'), is_contract=True)
        )
    )

    if search_query:
        users = users.filter(
            Q(first_name__icontains=search_query) |
            Q(last_name__icontains=search_query) |
            Q(username__icontains=search_query) |
            Q(phone__icontains=search_query) |
            Q(student_id__icontains=search_query)
        )

    if role_filter:
        users = users.filter(role=role_filter)
    if profession_filter:
        users = users.filter(profession_id=profession_filter)
    if status_filter == 'blocked':
        users = users.filter(is_blocked=True)
    elif status_filter == 'active':
        users = users.filter(is_blocked=False)
    if group_filter == 'none':
        # Heç qanday guruhga biriktirilmagan öquvçilar
        users = users.filter(role='student', study_groups__isnull=True)
    elif group_filter:
        # Guruh a'zolari (öquvçilar) böyiça filtrlaş — StudyGroup.students M2M
        users = users.filter(study_groups__id=group_filter).distinct()
    if payment_filter == 'contract':
        users = users.filter(has_contract=True)
    elif payment_filter == 'balance':
        users = users.filter(role='student', has_contract=False)
    if dob_filter == 'missing':
        users = users.filter(birth_date__isnull=True)
    elif dob_filter == 'has':
        users = users.filter(birth_date__isnull=False)

    if sort_by == 'name':
        users = users.order_by('first_name', 'last_name')
    elif sort_by == 'name_desc':
        users = users.order_by('-first_name', '-last_name')
    elif sort_by == 'newest':
        users = users.order_by('-date_joined')
    elif sort_by == 'oldest':
        users = users.order_by('date_joined')
    elif sort_by == 'activity':
        users = users.order_by('-last_activity')
    else:
        users = users.order_by('-date_joined')
    
    total_count = users.count()
    user_stats = CustomUser.objects.aggregate(
        students_count=Count('id', filter=Q(role='student')),
        teachers_count=Count('id', filter=Q(role='teacher')),
        blocked_count=Count('id', filter=Q(is_blocked=True)),
        contract_count=Count('id', filter=Q(student_balance__is_contract=True)),
        dob_missing_count=Count('id', filter=Q(role='student', birth_date__isnull=True)),
    )
    professions = Profession.objects.only('id', 'name')
    groups = StudyGroup.objects.select_related('profession').only(
        'id', 'name', 'profession__name'
    ).order_by('profession__name', 'name')

    paginator = Paginator(users, 25)
    page = request.GET.get('page')
    users = paginator.get_page(page)

    active_filters = sum([
        bool(role_filter),
        bool(profession_filter),
        bool(status_filter),
        bool(group_filter),
        bool(payment_filter),
        bool(dob_filter),
    ])

    from .users_ai import ai_available as _users_ai_available

    return render(request, 'accounts/admin/users.html', {
        'users': users,
        'professions': professions,
        'groups': groups,
        'search_query': search_query,
        'role_filter': role_filter,
        'profession_filter': profession_filter,
        'status_filter': status_filter,
        'group_filter': group_filter,
        'payment_filter': payment_filter,
        'dob_filter': dob_filter,
        'sort_by': sort_by,
        'total_count': total_count,
        'active_filters': active_filters,
        'ai_available': _users_ai_available(),
        **user_stats,
    })


@login_required
def admin_export_students(request):
    """Öquvçilar ma'lumotlarini Excel ga export qiliş - maydonlarni tanlaş"""
    if not request.user.is_admin:
        return redirect('home')
    
    # Available fields for export
    EXPORT_FIELDS = {
        'username': "Username",
        'first_name': "Ism",
        'last_name': "Familiya",
        'phone': "Telefon",
        'email': "Email",
        'birth_date': "Tuğilgan kuni",
        'gender': "Jinsi",
        'nationality': "Millati",
        'passport_series': "Pasport seriyasi",
        'passport_number': "Pasport raqami",
        'jshshir': "JŞŞIR (PINFL)",
        'passport_issued_date': "Pasport berilgan sana",
        'father_name': "Otasining ismi",
        'father_phone': "Otasining telefoni",
        'father_deceased': "Otasi vafot etgan",
        'mother_name': "Onasining ismi",
        'mother_phone': "Onasining telefoni",
        'mother_deceased': "Onasi vafot etgan",
        'parent_phone': "Ota-ona telefoni",
        'school_name': "Maktab nomi",
        'school_number': "Maktab raqami",
        'address': "Manzil",
        'profession__name': "Yönaliş",
        'role': "Rol",
        'coins': "Coinlar",
        'date_joined': "Röyxatdan ötgan",
        'last_activity': "Oxirgi faollik",
        'is_blocked': "Bloklangan",
        'is_premium': "Premium",
        'is_profile_completed': "Profil töliq",
        'telegram_username': "Telegram",
    }
    
    from .models import StudyGroup
    groups = StudyGroup.objects.select_related('profession').only(
        'id', 'name', 'profession__name'
    ).order_by('profession__name', 'name')
    selected_group = (request.GET.get('group', '') or request.POST.get('group', '')).strip()

    if request.method == 'POST':
        selected_fields = request.POST.getlist('fields')
        if not selected_fields:
            messages.error(request, "Kamida bitta maydon tanlang!")
            return render(request, 'accounts/admin/export_students.html', {
                'export_fields': EXPORT_FIELDS,
                'groups': groups,
                'selected_group': selected_group,
            })

        # Filter: only valid fields
        valid_fields = [f for f in selected_fields if f in EXPORT_FIELDS]
        if not valid_fields:
            messages.error(request, "Notöğri maydonlar tanlandi!")
            return redirect('admin_export_students')

        return _generate_students_excel(valid_fields, EXPORT_FIELDS, group_id=selected_group or None)

    return render(request, 'accounts/admin/export_students.html', {
        'export_fields': EXPORT_FIELDS,
        'groups': groups,
        'selected_group': selected_group,
    })


def _generate_students_excel(selected_fields, field_labels, group_id=None):
    """Tanlangan maydonlar asosida Excel fayl yaratiş.

    ``group_id`` berilsa — faqat öşa guruh (StudyGroup) a'zolari eksport
    qilinadi; aks holda barça öquvçilar."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from django.http import HttpResponse

    # Query students
    students = CustomUser.objects.filter(role='student').select_related('profession').order_by('first_name', 'last_name')
    if group_id:
        students = students.filter(study_groups__id=group_id).distinct()
    
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Öquvçilar"
    
    # Styles
    header_font = Font(name='Arial', bold=True, size=12, color='FFFFFF')
    header_fill = PatternFill(start_color='667eea', end_color='667eea', fill_type='solid')
    header_alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
    cell_alignment = Alignment(vertical='center', wrap_text=True)
    thin_border = Border(
        left=Side(style='thin'),
        right=Side(style='thin'),
        top=Side(style='thin'),
        bottom=Side(style='thin')
    )
    
    # Headers
    headers = [field_labels[f] for f in selected_fields]
    headers.insert(0, '№')
    
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_alignment
        cell.border = thin_border
    
    # Data
    for row_idx, student in enumerate(students, 2):
        # Row number
        cell = ws.cell(row=row_idx, column=1, value=row_idx - 1)
        cell.alignment = cell_alignment
        cell.border = thin_border
        
        for col_idx, field in enumerate(selected_fields, 2):
            if field == 'profession__name':
                value = student.profession.name if student.profession else '-'
            elif field == 'birth_date':
                value = student.birth_date.strftime('%d.%m.%Y') if student.birth_date else '-'
            elif field == 'date_joined':
                value = student.date_joined.strftime('%d.%m.%Y %H:%M') if student.date_joined else '-'
            elif field == 'last_activity':
                value = student.last_activity.strftime('%d.%m.%Y %H:%M') if student.last_activity else '-'
            elif field == 'passport_issued_date':
                value = student.passport_issued_date.strftime('%d.%m.%Y') if student.passport_issued_date else '-'
            elif field in ('is_blocked', 'is_premium', 'is_profile_completed', 'father_deceased', 'mother_deceased'):
                value = 'Ha' if getattr(student, field, False) else "Yöq"
            elif field == 'gender':
                value = student.get_gender_display() if student.gender else '-'
            elif field == 'role':
                value = student.get_role_display()
            else:
                value = getattr(student, field, '-') or '-'
            
            cell = ws.cell(row=row_idx, column=col_idx, value=str(value))
            cell.alignment = cell_alignment
            cell.border = thin_border
    
    # Auto-width
    for col in range(1, len(headers) + 1):
        max_len = 0
        for row in range(1, ws.max_row + 1):
            cell_value = str(ws.cell(row=row, column=col).value or '')
            max_len = max(max_len, len(cell_value))
        ws.column_dimensions[get_column_letter(col)].width = min(max_len + 4, 40)
    
    # Freeze header
    ws.freeze_panes = 'A2'
    
    # Response
    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    filename = f"students_export_{timezone.now().strftime('%Y%m%d_%H%M')}.xlsx"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    wb.save(response)
    return response


@login_required
def admin_user_view(request, pk):
    if not request.user.is_admin:
        return redirect('home')
    
    user = get_object_or_404(CustomUser, pk=pk)
    certificates = Certificate.objects.filter(student=user)
    
    # Öquvçi tahlili
    analysis_data = []
    
    if user.role == 'student':
        enrollments = user.enrollments.all().select_related('profession')
        
        for enrollment in enrollments:
            profession = enrollment.profession
            
            # Darslar
            total_lessons = Lesson.objects.filter(profession=profession).count()
            
            # Video progress
            watched_videos = VideoProgress.objects.filter(
                user=user,
                video__lesson__profession=profession,
                watched=True
            ).count()
            total_videos = VideoLesson.objects.filter(lesson__profession=profession).count()
            
            # Test natijalari
            test_results = TestResult.objects.filter(
                student=user,
                test__lesson__profession=profession
            )
            tests_taken = test_results.count()
            avg_test_score = test_results.aggregate(avg=Avg('score'))['avg'] or 0
            passed_tests = test_results.filter(passed=True).count()
            
            # Homework
            homework_submissions = HomeworkSubmission.objects.filter(
                student=user,
                homework__lesson__profession=profession
            )
            total_homeworks = Homework.objects.filter(lesson__profession=profession).count()
            submitted_homeworks = homework_submissions.count()
            graded_homeworks = homework_submissions.filter(status='graded').count()
            pending_homeworks = homework_submissions.filter(status='pending').count()
            avg_homework_grade = homework_submissions.filter(grade__isnull=False).aggregate(avg=Avg('grade'))['avg'] or 0
            
            # Umumiy progress
            completed_items = watched_videos + tests_taken + submitted_homeworks
            total_items = total_videos + Test.objects.filter(lesson__profession=profession).count() + total_homeworks
            overall_progress = (completed_items / total_items * 100) if total_items > 0 else 0
            
            # Qaysi darslarda töxtab qolgan
            stuck_lessons = []
            lessons = Lesson.objects.filter(profession=profession).order_by('section__order', 'order')
            for lesson in lessons:
                # Video lesson uçun tekşiriş (OneToOne relation)
                has_video = hasattr(lesson, 'video') and lesson.video is not None
                lesson_videos = 1 if has_video else 0
                watched = VideoProgress.objects.filter(user=user, video__lesson=lesson, watched=True).count()
                has_test = Test.objects.filter(lesson=lesson).exists()
                test_passed = TestResult.objects.filter(student=user, test__lesson=lesson).exists()
                has_hw = Homework.objects.filter(lesson=lesson).exists()
                hw_submitted = HomeworkSubmission.objects.filter(student=user, homework__lesson=lesson).exists()
                
                # Agar video körilmagan yoki test topşirilmagan bölsa
                if (lesson_videos > 0 and watched < lesson_videos) or (has_test and not test_passed) or (has_hw and not hw_submitted):
                    stuck_lessons.append({
                        'lesson': lesson,
                        'video_progress': f"{watched}/{lesson_videos}",
                        'test_done': test_passed if has_test else None,
                        'hw_done': hw_submitted if has_hw else None,
                    })
            
            analysis_data.append({
                'profession': profession,
                'enrollment': enrollment,
                'overall_progress': round(overall_progress, 1),
                'total_lessons': total_lessons,
                'watched_videos': watched_videos,
                'total_videos': total_videos,
                'tests_taken': tests_taken,
                'avg_test_score': round(avg_test_score, 1),
                'passed_tests': passed_tests,
                'total_homeworks': total_homeworks,
                'submitted_homeworks': submitted_homeworks,
                'graded_homeworks': graded_homeworks,
                'pending_homeworks': pending_homeworks,
                'avg_homework_grade': round(avg_homework_grade, 1),
                'stuck_lessons': stuck_lessons[:5],  # Oxirgi 5 ta
            })
    
    # Kunlik limit boşqaruvi (öquvçi öqiyotgan kurslar böyiça)
    from .exam_admin import final_exam_summary
    daily_limit_courses = []
    if user.role == 'student':
        today = timezone.localdate()
        for enrollment in user.enrollments.all().select_related('profession'):
            today_access = DailySectionAccess.objects.filter(
                user=user,
                profession=enrollment.profession,
                accessed_date=today,
            ).select_related('section').first()

            if enrollment.daily_limit_override is True:
                mode = 'on'
            elif enrollment.daily_limit_override is False:
                mode = 'off'
            else:
                mode = 'inherit'

            daily_limit_courses.append({
                'enrollment': enrollment,
                'profession': enrollment.profession,
                'mode': mode,
                'course_default': enrollment.profession.daily_section_limit,
                'is_active': enrollment.daily_limit_effective,
                'today_section': today_access.section if today_access else None,
                'final_exams': final_exam_summary(user, enrollment.profession),
            })

    # Coin statistikasi
    coin_earned = CoinTransaction.objects.filter(user=user, action='add').aggregate(total=Sum('amount'))['total'] or 0
    coin_spent = CoinTransaction.objects.filter(user=user, action__in=['remove', 'purchase']).aggregate(total=Sum('amount'))['total'] or 0
    
    # Oxirgi faolliklar
    recent_activities = ActivityLog.objects.filter(user=user).order_by('-created_at')[:10]
    login_locations = Paginator(user.login_locations.all(), 10).get_page(request.GET.get('locations_page', 1))

    # Startup / ğoya xulosasi. Lokal import — `accounts` app'i `startup` ga
    # modul darajasida boğlanib qolmasin (aylanma importdan himoya).
    # Rol böyiça filtr yöq: funksiya startup ham, a'zolik ham bölmasa
    # `None` qaytaradi, ya'ni şablon tab'ni özi çizmaydi.
    from startup.services import student_startup_summary
    startup_summary = student_startup_summary(user)

    return render(request, 'accounts/admin/user_view.html', {
        'user_obj': user,
        'certificates': certificates,
        'analysis_data': analysis_data,
        'daily_limit_courses': daily_limit_courses,
        'coin_earned': coin_earned,
        'coin_spent': coin_spent,
        'recent_activities': recent_activities,
        'login_locations': login_locations,
        'startup_summary': startup_summary,
    })


@login_required
def admin_add_teacher(request):
    """Öqituvçi qöşiş"""
    if not request.user.is_admin:
        return redirect('home')
    
    professions = Profession.objects.all()
    
    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        username = request.POST.get('username', '').strip()
        phone = request.POST.get('phone', '').strip()
        password = request.POST.get('password', '').strip()
        selected_professions = request.POST.getlist('professions')
        
        # Validation
        if not all([first_name, last_name, username, phone, password]):
            messages.error(request, "Barça maydonlarni töldiring!")
            return render(request, 'accounts/admin/add_teacher.html', {
                'professions': professions,
            })
        
        if CustomUser.objects.filter(username=username).exists():
            messages.error(request, "Bu username allaqaçon band!")
            return render(request, 'accounts/admin/add_teacher.html', {
                'professions': professions,
            })
        
        if CustomUser.objects.filter(phone=phone).exists():
            messages.error(request, "Bu telefon raqam allaqaçon röyxatdan ötgan!")
            return render(request, 'accounts/admin/add_teacher.html', {
                'professions': professions,
            })
        
        teacher = CustomUser.objects.create_user(
            username=username,
            password=password,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            role='teacher',
        )
        
        # Assign teaching professions
        if selected_professions:
            teacher.teaching_professions.set(selected_professions)
            # Set primary profession to first selected
            teacher.profession = Profession.objects.filter(pk=selected_professions[0]).first()
            teacher.save()
        
        # Save certificates
        cert_titles = request.POST.getlist('cert_title')
        cert_files = request.FILES.getlist('cert_file')
        if cert_titles and cert_files:
            from teacher.models import TeacherCertificate
            for title, file in zip(cert_titles, cert_files):
                if title.strip() and file:
                    TeacherCertificate.objects.create(
                        teacher=teacher,
                        title=title.strip(),
                        file=file,
                    )
        
        messages.success(request, f"Öqituvçi {teacher.full_name} muvaffaqiyatli qöşildi!")
        return redirect('admin_users')
    
    return render(request, 'accounts/admin/add_teacher.html', {
        'professions': professions,
    })


@login_required
def admin_add_assistant_admin(request):
    """Yordamçi admin (assistant_admin) qöşiş - faqat admin uçun"""
    if not request.user.is_admin:
        return redirect('home')

    from accounts.models import AssistantRole
    roles = AssistantRole.objects.filter(is_active=True).order_by('name')

    context = {'roles': roles}

    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        username = request.POST.get('username', '').strip()
        phone = request.POST.get('phone', '').strip()
        password = request.POST.get('password', '').strip()
        role_id = request.POST.get('assistant_role', '').strip()

        if not all([first_name, last_name, username, phone, password]):
            messages.error(request, "Barça maydonlarni töldiring!")
            return render(request, 'accounts/admin/add_assistant_admin.html', context)

        if len(password) < 6:
            messages.error(request, "Parol kamida 6 belgi bölişi kerak!")
            return render(request, 'accounts/admin/add_assistant_admin.html', context)

        if CustomUser.objects.filter(username=username).exists():
            messages.error(request, "Bu username allaqaçon band!")
            return render(request, 'accounts/admin/add_assistant_admin.html', context)

        if CustomUser.objects.filter(phone=phone).exists():
            messages.error(request, "Bu telefon raqam allaqaçon röyxatdan ötgan!")
            return render(request, 'accounts/admin/add_assistant_admin.html', context)

        assistant_role_obj = None
        if role_id:
            assistant_role_obj = AssistantRole.objects.filter(pk=role_id, is_active=True).first()

        assistant = CustomUser.objects.create_user(
            username=username,
            password=password,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            role='assistant_admin',
            assistant_role=assistant_role_obj,
        )
        messages.success(request, f"Yordamçi admin {assistant.full_name} muvaffaqiyatli qöşildi!")
        return redirect('admin_users')

    return render(request, 'accounts/admin/add_assistant_admin.html', context)


@login_required
def assistant_admin_dashboard(request):
    """Yordamçi admin uçun alohida boşqaruv sahifasi.

    Sahifa API orqali (api/assistant/...) ma'lumotlarni yuklab boşqaradi.
    Faqat admin yoki assistant_admin kira oladi.
    """
    if not (request.user.is_admin or request.user.is_assistant_admin):
        return redirect('home')

    # API uçun token tayyorlaş
    from rest_framework.authtoken.models import Token
    token, _ = Token.objects.get_or_create(user=request.user)

    return render(request, 'accounts/admin/assistant_dashboard.html', {
        'api_token': token.key,
    })


@login_required
def admin_user_edit(request, pk):
    if not request.user.is_admin:
        return redirect('home')
    
    user = get_object_or_404(CustomUser.objects.select_related('profession'), pk=pk)
    
    professions = Profession.objects.only('id', 'name')
    
    if request.method == 'POST':
        form = AdminUserEditForm(request.POST, request.FILES, instance=user)
        if form.is_valid():
            user = form.save(commit=False)
            new_password = form.cleaned_data.get('new_password')
            if new_password:
                user.set_password(new_password)
            changed_fields = [f for f in form.changed_data if f != 'new_password']
            if new_password:
                changed_fields.append('password')
            if changed_fields:
                user.save(update_fields=changed_fields)
            
            # Öqituvçi kurslarini yangilaş
            if user.role == 'teacher':
                selected_professions = request.POST.getlist('teaching_professions')
                user.teaching_professions.set(selected_professions)
            
            messages.success(request, "Foydalanuvçi muvaffaqiyatli yangilandi!")
            return redirect('admin_users')
    else:
        form = AdminUserEditForm(instance=user)
    
    return render(request, 'accounts/admin/user_edit.html', {
        'form': form,
        'user_obj': user,
        'professions': professions,
    })


@login_required
def admin_user_block(request, pk):
    if not request.user.is_admin:
        return redirect('home')
    
    user = get_object_or_404(
        CustomUser.objects.only('id', 'first_name', 'last_name', 'role', 'is_blocked', 'telegram_id'),
        pk=pk
    )
    
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    if user.is_admin:
        if is_ajax:
            return JsonResponse({'success': False, 'error': "Admin foydalanuvçini bloklaş mumkin emas!"}, status=403)
        messages.error(request, "Admin foydalanuvçini bloklaş mumkin emas!")
        return redirect('admin_users')
    
    user.is_blocked = not user.is_blocked
    user.save(update_fields=['is_blocked'])
    
    if user.is_blocked:
        notify_group(
            f"🚫 <b>Foydalanuvçi bloklandi!</b>\n\n"
            f"👤 {user.full_name}\n"
            f"📋 Role: {user.get_role_display()}\n"
            f"🔒 Admin <b>{request.user.full_name}</b> tomonidan bloklandi."
        )
        if user.telegram_id:
            notify_user(
                user.telegram_id,
                f"🚫 <b>Hisobingiz bloklandi!</b>\n\n"
                f"👤 {user.full_name}\n"
                f"📋 Rol: {user.get_role_display()}\n\n"
                f"❌ Sizning hisobingiz admin tomonidan bloklandi.\n"
                f"📞 Qöşimça ma'lumot uçun adminstratsiya bilan boğlaning."
            )
    else:
        notify_group(
            f"✅ <b>Foydalanuvçi blokdan çiqarildi!</b>\n\n"
            f"👤 {user.full_name}\n"
            f"📋 Role: {user.get_role_display()}\n"
            f"🔓 Admin <b>{request.user.full_name}</b> tomonidan blokdan çiqarildi."
        )
        if user.telegram_id:
            notify_user(
                user.telegram_id,
                f"✅ <b>Hisobingiz blokdan çiqarildi!</b>\n\n"
                f"👤 {user.full_name}\n\n"
                f"🎉 Hisobingiz oçildi. Endi tizimdan foydalanişingiz mumkin."
            )
    
    if is_ajax:
        return JsonResponse({
            'success': True,
            'is_blocked': user.is_blocked,
            'message': f"{user.full_name} {'bloklandi' if user.is_blocked else 'blokdan chiqarildi'}!",
        })

    messages.success(request, f"{user.full_name} {'bloklandi' if user.is_blocked else 'blokdan chiqarildi'}!")
    return redirect('admin_users')


@login_required
def admin_unlink_telegram(request, pk):
    """Admin tomonidan foydalanuvçining Telegram ulanişini KODSIZ uziş.

    Oddiy uziş (profil sahifasidan) tasdiqlaş kodini aynan ulangan
    Telegram akkauntiga yuboradi. Agar foydalanuvçi şu Telegram
    akkauntiga endi kira olmasa (yöqotgan, almaştirgan va h.k.), kodni
    olişning iloji bölmaydi — şu sabab faqat admin panel orqali,
    kodsiz, majburiy uziş imkoniyati beriladi.
    """
    if not request.user.is_admin:
        messages.error(request, "Sizda bu amalni bajariş huquqi yöq!")
        return redirect('home')

    if request.method != 'POST':
        return redirect('admin_user_view', pk=pk)

    user = get_object_or_404(CustomUser, pk=pk)

    if not user.telegram_id:
        messages.error(request, "Bu foydalanuvçida ulangan Telegram yöq!")
        return redirect('admin_user_view', pk=pk)

    old_username = user.telegram_username

    # Foydalanuvçining eski (agar bölsa) tasdiqlaş kodi keşini ham tozalaymiz
    cache.delete(f'tg_unlink_code_{user.id}')

    user.telegram_id = None
    user.telegram_username = None
    user.save(update_fields=['telegram_id', 'telegram_username'])

    notify_group(
        f"🔓 <b>Telegram majburiy uzildi (admin tomonidan)</b>\n\n"
        f"👤 {user.full_name}\n"
        f"📋 Role: {user.get_role_display()}\n"
        f"📎 Avvalgi username: {'@' + old_username if old_username else '—'}\n"
        f"🛠 Admin: {request.user.full_name}\n"
        f"❗ Tasdiqlaş kodisiz, admin panel orqali uzildi."
    )

    messages.success(request, f"{user.full_name} ning Telegram ulanişi muvaffaqiyatli uzildi! 🔓")
    return redirect('admin_user_view', pk=pk)


@login_required
def admin_user_finished(request, pk):
    """Öquvçini 'tugatgan' deb belgilash/qaytarish.
    Tugatgan öquvçilar yangi kurslarga yozila olmaydi va tölovlar sahifasida körinmaydi.
    """
    if not request.user.is_admin:
        return redirect('home')

    user = get_object_or_404(
        CustomUser.objects.only('id', 'first_name', 'last_name', 'role', 'is_finished', 'telegram_id'),
        pk=pk
    )

    if user.role != 'student':
        messages.error(request, "Faqat öquvçini tugatgan deb belgilaş mumkin!")
        return redirect('admin_users')

    user.is_finished = not user.is_finished
    user.save(update_fields=['is_finished'])

    if user.is_finished:
        messages.success(request, f"{user.full_name} 'kursni tugatgan' deb belgilandi. Endi yangi kurslarga yozila olmaydi va tölovlar röyxatida körinmaydi.")
        if user.telegram_id:
            notify_user(
                user.telegram_id,
                f"🎓 <b>Kursni tugatgan deb belgilandingiz!</b>\n\n"
                f"👤 {user.full_name}\n\n"
                f"✅ Tabriklaymiz! Endi siz yangi kurslarga yozila olmaysiz va tölovlar röyxatidan çiqarildingiz."
            )
    else:
        messages.success(request, f"{user.full_name} 'tugatgan' belgisi olib taşlandi. Endi yana yangi kurslarga yozilişi va tölovlar sahifasida körinişi mumkin.")

    return redirect('admin_users')


@login_required
def admin_user_delete(request, pk):
    if not request.user.is_admin:
        return redirect('home')
    
    user = get_object_or_404(CustomUser, pk=pk)
    
    if user.is_admin:
        messages.error(request, "Admin foydalanuvçini öçiriş mumkin emas!")
        return redirect('admin_users')
    
    if request.method == 'POST':
        user.delete()
        messages.success(request, "Foydalanuvçi öçirildi!")
        return redirect('admin_users')
    
    return render(request, 'accounts/admin/user_delete.html', {'user_obj': user})


MONTH_NAMES_UZ = [
    '', 'Yanvar', 'Fevral', 'Mart', 'Aprel', 'May', 'Iyun',
    'Iyul', 'Avgust', 'Sentyabr', 'Oktyabr', 'Noyabr', 'Dekabr'
]


def _parse_stats_filters(request):
    """Analitika sahifasi uçun yil / oy / kurs yönalişi (profession) filtrlarini
    GET parametrlaridan öqib, sana oraliğini hisoblaydi.

    Qaytaradi: dict(year, month, profession_id, profession, start_date, end_date,
                    label, query_string)
    """
    today = timezone.now().date()

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    year = _int(request.GET.get('year'))
    month = _int(request.GET.get('month'))
    profession_id = _int(request.GET.get('profession'))

    if month is not None and not (1 <= month <= 12):
        month = None
    if year is not None and not (2000 <= year <= today.year + 1):
        year = None

    profession = None
    if profession_id:
        profession = Profession.objects.filter(pk=profession_id).first()
        if not profession:
            profession_id = None

    # ── Sana oraliği (start <= x < end) ──
    start_date = end_date = None
    if year and month:
        start_date = date(year, month, 1)
        end_date = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    elif year:
        start_date = date(year, 1, 1)
        end_date = date(year + 1, 1, 1)
    elif month:
        y = today.year
        start_date = date(y, month, 1)
        end_date = date(y + 1, 1, 1) if month == 12 else date(y, month + 1, 1)

    # ── Inson öqiy oladigan yorliq ──
    label_parts = []
    if profession:
        label_parts.append(profession.name)
    if month:
        label_parts.append(MONTH_NAMES_UZ[month])
    if year:
        label_parts.append(str(year))
    elif month:
        label_parts.append(str(today.year))
    label = " · ".join(label_parts) if label_parts else "Umumiy (barça davr)"

    # ── PDF havolasi uçun query string ──
    qs_parts = []
    if year:
        qs_parts.append(f"year={year}")
    if month:
        qs_parts.append(f"month={month}")
    if profession_id:
        qs_parts.append(f"profession={profession_id}")
    query_string = "&".join(qs_parts)

    return {
        'year': year,
        'month': month,
        'profession_id': profession_id,
        'profession': profession,
        'start_date': start_date,
        'end_date': end_date,
        'label': label,
        'query_string': query_string,
    }


def _filter_options(selected):
    """Filtr dropdownlari uçun yil / oy / kurs röyxatini tayyorlaydi."""
    today = timezone.now().date()

    years = set()
    first_join = CustomUser.objects.order_by('date_joined').values_list('date_joined', flat=True).first()
    start_year = first_join.year if first_join else today.year
    for y in range(start_year, today.year + 1):
        years.add(y)
    years = sorted(years, reverse=True)

    months = [{'num': m, 'name': MONTH_NAMES_UZ[m]} for m in range(1, 13)]

    professions = list(Profession.objects.order_by('name').values('id', 'name'))

    return {
        'filter_years': years,
        'filter_months': months,
        'filter_professions': professions,
        'selected_year': selected['year'],
        'selected_month': selected['month'],
        'selected_profession': selected['profession_id'],
        'filter_label': selected['label'],
        'filter_query': selected['query_string'],
        'filter_active': bool(selected['year'] or selected['month'] or selected['profession_id']),
    }




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
        ['Körsatkiç', 'Soni'],
        ['Jami foydalanuvçilar', str(total_users)],
        ['Öquvçilar', str(total_students)],
        ['Öqituvçilar', str(total_teachers)],
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
    
    if request.user.is_teacher and not request.user.teaching_professions.filter(pk=profession.pk).exists():
        messages.error(request, "Sizda bu kursga kiriş huquqi yöq!")
        return redirect('professions')
    
    if request.method == 'POST':
        form = SectionForm(request.POST)
        if form.is_valid():
            section = form.save(commit=False)
            section.profession = profession
            section.save()
            messages.success(request, "Bölim muvaffaqiyatli qöşildi!")
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
    
    if request.user.is_teacher and not request.user.teaching_professions.filter(pk=section.profession.pk).exists():
        messages.error(request, "Sizda bu kursga kiriş huquqi yöq!")
        return redirect('professions')
    
    if request.method == 'POST':
        form = SectionForm(request.POST, instance=section)
        if form.is_valid():
            form.save()
            messages.success(request, "Bölim muvaffaqiyatli yangilandi!")
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
    
    if request.user.is_teacher and not request.user.teaching_professions.filter(pk=section.profession.pk).exists():
        messages.error(request, "Sizda bu kursga kiriş huquqi yöq!")
        return redirect('professions')
    
    profession_pk = section.profession.pk
    
    if request.method == 'POST':
        section.delete()
        messages.success(request, "Bölim öçirildi!")
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
    
    paginator = Paginator(sections, 25)
    page = request.GET.get('page')
    sections = paginator.get_page(page)
    
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
            messages.success(request, "Bölim qöşildi!")
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
        messages.success(request, "Bölim yangilandi!")
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
        messages.success(request, "Bölim öçirildi!")
        return redirect('admin_sections')
    
    return render(request, 'accounts/admin/section_delete.html', {'section': section})


# ==================== COIN BOŞQARUVI ====================
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.shortcuts import render, redirect
from django.core.paginator import Paginator
from django.db import transaction

@login_required
def admin_manage_coins(request):

    # 🔐 Admin check
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kiriş huquqi yöq!")
        return redirect('home')

    # 📌 Studentlar queryset
    users_qs = CustomUser.objects.filter(
        role='student'
    ).order_by('-coins', 'first_name')

    # =========================
    # FILTRLAR
    # =========================
    search_q = request.GET.get('q', '').strip()
    course_filter = request.GET.get('course', '')
    coin_filter = request.GET.get('coin_range', '')
    sort_by = request.GET.get('sort', '-coins')

    if search_q:
        users_qs = users_qs.filter(
            models.Q(first_name__icontains=search_q) |
            models.Q(last_name__icontains=search_q) |
            models.Q(username__icontains=search_q) |
            models.Q(phone__icontains=search_q)
        )

    if course_filter:
        users_qs = users_qs.filter(enrollments__profession_id=course_filter).distinct()

    if coin_filter == '0':
        users_qs = users_qs.filter(coins=0)
    elif coin_filter == '1-50':
        users_qs = users_qs.filter(coins__gte=1, coins__lte=50)
    elif coin_filter == '51-200':
        users_qs = users_qs.filter(coins__gte=51, coins__lte=200)
    elif coin_filter == '200+':
        users_qs = users_qs.filter(coins__gt=200)

    if sort_by == 'coins':
        users_qs = users_qs.order_by('coins', 'first_name')
    elif sort_by == '-coins':
        users_qs = users_qs.order_by('-coins', 'first_name')
    elif sort_by == 'name':
        users_qs = users_qs.order_by('first_name', 'last_name')
    elif sort_by == '-name':
        users_qs = users_qs.order_by('-first_name', '-last_name')

    # =========================
    # POST ACTION (Coin berish/ayirish)
    # =========================
    if request.method == 'POST':

        user_id = request.POST.get('user_id')
        amount = request.POST.get('amount')
        action = request.POST.get('action')
        reason = request.POST.get('reason', '').strip()
        bulk_ids = request.POST.getlist('bulk_user_ids')

        try:
            amount = int(amount)
            if amount <= 0:
                raise ValueError("Amount must be positive")

            # Guruhga coin beriş
            if bulk_ids and action in ('add', 'remove'):
                with transaction.atomic():
                    bulk_users = CustomUser.objects.filter(pk__in=bulk_ids).select_for_update()
                    count = 0
                    for u in bulk_users:
                        if action == 'add':
                            u.coins += amount
                        else:
                            u.coins -= amount
                        u.save(update_fields=['coins'])
                        CoinTransaction.objects.create(
                            user=u,
                            amount=amount,
                            action=action,
                            reason=reason or (
                                "Admin tomonidan coin berildi"
                                if action == 'add'
                                else "Admin tomonidan coin ayirildi"
                            ),
                        )
                        text = f"Sizga {amount} coin berildi! 🎉" if action == 'add' else f"Sizdan {amount} coin ayirildi."
                        if reason:
                            text += f"\n\nSabab: {reason}"
                        Message.objects.create(
                            sender=request.user, recipient=u,
                            title="Coin rağbatlantiriş" if action == 'add' else "Coin ayirildi",
                            content=text, message_type='system'
                        )
                        count += 1
                    verb = "berildi" if action == 'add' else "ayirildi"
                    emoji = "🎁" if action == 'add' else "📤"
                    reason_text = f"\n📝 Sabab: {reason}" if reason else ""
                    coin_msg = (
                        f"{emoji} <b>Coin {verb}!</b>\n\n"
                        f"👥 {count} ta öquvçiga {amount} coin {verb}{reason_text}"
                    )
                    notify_admins(coin_msg)
                    notify_group(coin_msg)
                    messages.success(request, f"{count} ta öquvçiga {amount} coin {verb}!")

            elif user_id:
                with transaction.atomic():
                    user = CustomUser.objects.select_for_update().get(pk=user_id)
                    if action == 'add':
                        user.coins += amount
                        user.save(update_fields=['coins'])
                        CoinTransaction.objects.create(
                            user=user,
                            amount=amount,
                            action='add',
                            reason=reason or "Admin tomonidan coin berildi",
                        )
                        text = f"Sizga {amount} coin rağbatlantiriş sifatida berildi!"
                        if reason:
                            text += f"\n\nSabab: {reason}"
                        Message.objects.create(
                            sender=request.user, recipient=user,
                            title="Coin rağbatlantiriş", content=text, message_type='system'
                        )
                        coin_msg = (
                            f"🎁 <b>Coin berildi!</b>\n\n"
                            f"👤 {user.first_name} {user.last_name}\n"
                            f"🪙 +{amount} coin (Jami: {user.coins})\n"
                            f"📝 Sabab: {reason or 'belgilanmagan'}"
                        )
                        notify_admins(coin_msg)
                        notify_group(coin_msg)
                        notify_user(user.telegram_id, coin_msg)
                        messages.success(request, f"{user.full_name}ga {amount} coin berildi!")
                    elif action == 'remove':
                        user.coins -= amount
                        user.save(update_fields=['coins'])
                        CoinTransaction.objects.create(
                            user=user,
                            amount=amount,
                            action='remove',
                            reason=reason or "Admin tomonidan coin ayirildi",
                        )
                        text = f"Sizdan {amount} coin ayirildi."
                        if reason:
                            text += f"\n\nSabab: {reason}"
                        Message.objects.create(
                            sender=request.user, recipient=user,
                            title="Coin ayirildi", content=text, message_type='system'
                        )
                        coin_msg = (
                            f"📤 <b>Coin ayirildi!</b>\n\n"
                            f"👤 {user.first_name} {user.last_name}\n"
                            f"🪙 -{amount} coin (Qoldi: {user.coins})\n"
                            f"📝 Sabab: {reason or 'belgilanmagan'}"
                        )
                        notify_admins(coin_msg)
                        notify_group(coin_msg)
                        notify_user(user.telegram_id, coin_msg)
                        messages.success(request, f"{user.full_name}dan {amount} coin ayirildi! (Qoldi: {user.coins})")

        except (ValueError, CustomUser.DoesNotExist):
            messages.error(request, "Xatolik yuz berdi!")

        # 🔥 Qaysi sahifada bölsa öşa sahifaga qaytadi
        params = request.GET.urlencode()
        if params:
            return redirect(f"{request.path}?{params}")
        return redirect('admin_manage_coins')

    # =========================
    # STATISTIKA
    # =========================
    all_students = CustomUser.objects.filter(role='student')
    total_coins = all_students.aggregate(total=models.Sum('coins'))['total'] or 0
    avg_coins = all_students.aggregate(avg=models.Avg('coins'))['avg'] or 0
    max_coins_user = all_students.order_by('-coins').first()
    zero_coin_count = all_students.filter(coins=0).count()

    stats = {
        'total_students': all_students.count(),
        'total_coins': total_coins,
        'avg_coins': round(avg_coins, 1),
        'max_coins': max_coins_user.coins if max_coins_user else 0,
        'max_coins_user': max_coins_user,
        'zero_coin_count': zero_coin_count,
    }

    courses = Profession.objects.all().order_by('name')

    # =========================
    # PAGINATION
    # =========================
    paginator = Paginator(users_qs, 25)
    page_number = request.GET.get('page')
    users = paginator.get_page(page_number)

    context = {
        'users': users,
        'stats': stats,
        'courses': courses,
        'search_q': search_q,
        'course_filter': course_filter,
        'coin_filter': coin_filter,
        'sort_by': sort_by,
    }

    return render(request, 'accounts/admin/manage_coins.html', context)


# ==================== YORDAM SÖROVLARI ====================

@login_required
def help_page(request):
    my_requests = HelpRequest.objects.filter(user=request.user).order_by('-created_at')
    return render(request, 'accounts/help_page.html', {
        'my_requests': my_requests,
    })


@login_required
def submit_help_request(request):
    if request.method == 'POST':
        import logging
        from django.utils import timezone
        from datetime import timedelta
        logger = logging.getLogger(__name__)

        subject = request.POST.get('subject', '').strip()
        message = request.POST.get('message', '').strip()
        image = request.FILES.get('image')
        
        logger.info(f"Help Request Received - User: {request.user.username}, Subject: {subject}, Image: {image}")

        if not subject or not message:
            return JsonResponse({'success': False, 'error': "Mavzu va xabar töldirilişi şart!"})

        # Backend Debounce: Check for recent duplicate
        recent_duplicate = HelpRequest.objects.filter(
            user=request.user,
            subject=subject,
            created_at__gte=timezone.now() - timedelta(seconds=10)
        ).exists()

        if recent_duplicate:
            logger.warning(f"Duplicate request detected for user {request.user.username}")
            return JsonResponse({'success': False, 'error': "Siz bu xabarni yaqinda yubordingiz. Biroz kuting."})

        
        help_request = HelpRequest.objects.create(
            user=request.user,
            subject=subject,
            message=message,
            image=image
        )
        notify_admins(
            f"🆘 <b>Yangi yordam sörovi!</b>\n\n"
            f"👤 {request.user.first_name} {request.user.last_name}\n"
            f"📌 {subject}\n"
            f"💬 {message[:100]}"
        )
        
        logger.info(f"Help Request Created - ID: {help_request.id}, Image Path: {help_request.image.name if help_request.image else 'None'}")
        
        return JsonResponse({'success': True, 'message': "Xabaringiz yuborildi! Tez orada javob beramiz."})
    
    return JsonResponse({'success': False, 'error': 'Notöğri sörov'})


@login_required
def admin_help_requests(request):
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kiriş huquqi yöq!")
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
        messages.error(request, "Sizda bu sahifaga kiriş huquqi yöq!")
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
        
        messages.success(request, "Sörov yangilandi!")
        return redirect('admin_help_request_detail', pk=pk)
    
    return render(request, 'accounts/admin/help_request_detail.html', {'help_request': help_request})


# ==================== TÖLOV ESLATMASI ====================

@login_required
def send_payment_reminder(request, pk):
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kiriş huquqi yöq!")
        return redirect('home')
    
    student = get_object_or_404(CustomUser, pk=pk, role='student')
    
    Message.objects.create(
        sender=request.user,
        recipient=student,
        title="Tölov eslatmasi",
        content=f"Hurmatli {student.full_name},\n\nSizning oylik tölovingiz muddati yaqinlaşmoqda. Iltimos, öz vaqtida tölov qilişni unutmang.\n\nHurmat bilan,\nIT Creative jamoasi",
        message_type='payment'
    )
    notify_user(
        student.telegram_id,
        f"💳 <b>Tölov eslatmasi</b>\n\n"
        f"Hurmatli {student.full_name},\n\n"
        f"Sizning oylik tölovingiz muddati yaqinlaşmoqda.\n"
        f"Iltimos, öz vaqtida tölov qilişni unutmang.\n\n"
        f"🏫 IT Creative jamoasi"
    )
    notify_group(
        f"💳 <b>Tölov eslatmasi yuborildi</b>\n\n"
        f"👤 {student.full_name}ga tölov eslatmasi yuborildi."
    )
    
    messages.success(request, f"{student.full_name}ga tölov eslatmasi yuborildi!")
    return redirect('admin_payments')


@login_required
def send_bulk_payment_reminders(request):
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kiriş huquqi yöq!")
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
                title="Tölov eslatmasi",
                content=f"Hurmatli {student.full_name},\n\nSizning oylik tölovingiz muddati yaqinlaşmoqda. Iltimos, öz vaqtida tölov qilişni unutmang.\n\nHurmat bilan,\nIT Creative jamoasi",
                message_type='payment'
            )
            notify_user(
                student.telegram_id,
                f"💳 <b>Tölov eslatmasi</b>\n\n"
                f"Hurmatli {student.full_name},\n\n"
                f"Sizning oylik tölovingiz muddati yaqinlaşmoqda.\n"
                f"Iltimos, öz vaqtida tölov qilişni unutmang.\n\n"
                f"🏫 IT Creative jamoasi"
            )
            count += 1
        
        notify_group(
            f"💳 <b>Ommaviy tölov eslatmasi</b>\n\n"
            f"📊 {count} ta öquvçiga tölov eslatmasi yuborildi."
        )
        messages.success(request, f"{count} ta öquvçiga tölov eslatmasi yuborildi!")
    
    return redirect('admin_payments')


# ==================== ÇEGIRMALAR (SKIDKALAR) ====================

@login_required
def admin_discounts(request):
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kiriş huquqi yöq!")
        return redirect('home')
    
    from coin.discount_management import manage
    coin_context = manage(request)
    if not isinstance(coin_context, dict):
        return coin_context
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

    from .course_pricing import discounted_price
    discounts = list(discounts)
    for d in discounts:
        # Röyxatda "90 000 → 50 000" körinişi uçun.
        if d.profession and d.profession.is_sold_once:
            d.price_preview = (d.profession.one_time_price, discounted_price(int(d.profession.one_time_price), d))
    
    professions = Profession.objects.all()
    
    context = {
        'discounts': discounts,
        'professions': professions,
        'total_discounts': Discount.objects.count(),
        'active_discounts': Discount.objects.filter(is_active=True).count(),
        'inactive_discounts': Discount.objects.filter(is_active=False).count(),
    }
    context.update(coin_context)
    return render(request, 'accounts/admin/discounts.html', context)


def _course_discount_form(request, discount=None):
    from .course_discounts import CourseDiscountForm, announce
    if request.method == 'POST':
        form = CourseDiscountForm(request.POST, instance=discount)
        if form.is_valid():
            with transaction.atomic():
                saved = form.save()
                if form.cleaned_data.get('announce') and saved.is_valid:
                    announce(saved, request.user)
            messages.success(request, "Çegirma saqlandi!" if discount else "Çegirma muvaffaqiyatli qöşildi!")
            return redirect('admin_discounts')
    else:
        form = CourseDiscountForm(instance=discount, initial=None if discount else {
            'profession': request.GET.get('profession') or None, 'is_active': True})
    prices = {p.pk: int(p.one_time_price) for p in form.fields['profession'].queryset}
    return render(request, 'accounts/admin/discount_form.html', {
        'discount': discount, 'form': form, 'course_prices': prices,
    })


@login_required
def admin_discount_add(request):
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kiriş huquqi yöq!")
        return redirect('home')
    return _course_discount_form(request)


@login_required
def admin_discount_edit(request, pk):
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kiriş huquqi yöq!")
        return redirect('home')
    return _course_discount_form(request, get_object_or_404(Discount, pk=pk))


@login_required
def admin_discount_delete(request, pk):
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kiriş huquqi yöq!")
        return redirect('home')
    
    discount = get_object_or_404(Discount, pk=pk)
    
    if request.method == 'POST':
        discount.delete()
        messages.success(request, "Çegirma öçirildi!")
        return redirect('admin_discounts')
    
    return render(request, 'accounts/admin/discount_delete.html', {'discount': discount})


# ==================== ADMIN: DARSLAR STATISTIKASI ====================

@login_required
def admin_lesson_statistics(request):
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kiriş huquqi yöq!")
        return redirect('home')

    profession_id = request.GET.get('profession')
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    search_query = request.GET.get('q', '')
    year_param = request.GET.get('year', '')
    month_param = request.GET.get('month', '')
    quick_range = request.GET.get('range', '')  # today, week, month, year

    # Sana filterlari
    if date_from:
        try:
            date_from = timezone.datetime.strptime(date_from, '%Y-%m-%d').date()
        except:
            date_from = None
    if date_to:
        try:
            date_to = timezone.datetime.strptime(date_to, '%Y-%m-%d').date()
        except:
            date_to = None

    # Yil / Oy filterlar (Yil+Oy tanlansa oy oraliğiga; faqat yil bölsa yil oraliğiga)
    year_int = _to_int(year_param, 0) or None
    month_int = _to_int(month_param, 0) or None
    today = timezone.now().date()

    if quick_range == 'today':
        date_from = today
        date_to = today
    elif quick_range == 'week':
        date_from = today - timedelta(days=6)
        date_to = today
    elif quick_range == 'month':
        date_from = today.replace(day=1)
        date_to = today
    elif quick_range == 'year':
        date_from = today.replace(month=1, day=1)
        date_to = today
    elif year_int and month_int:
        try:
            date_from = date(year_int, month_int, 1)
            if month_int == 12:
                date_to = date(year_int, 12, 31)
            else:
                date_to = date(year_int, month_int + 1, 1) - timedelta(days=1)
        except ValueError:
            pass
    elif year_int:
        try:
            date_from = date(year_int, 1, 1)
            date_to = date(year_int, 12, 31)
        except ValueError:
            pass
    elif month_int and not year_int:
        # Faqat oy tanlansa, joriy yilning şu oyi
        try:
            date_from = date(today.year, month_int, 1)
            if month_int == 12:
                date_to = date(today.year, 12, 31)
            else:
                date_to = date(today.year, month_int + 1, 1) - timedelta(days=1)
        except ValueError:
            pass
    
    # ==================== UMUMIY STATISTIKA ====================
    total_lessons = Lesson.objects.count()
    total_video_lessons = Lesson.objects.filter(lesson_type='video').count()
    total_test_lessons = Lesson.objects.filter(lesson_type='test').count()
    total_homework_lessons = Lesson.objects.filter(lesson_type='homework').count()
    
    # Faol öquvçilar (oxirgi 7 kunda)
    week_ago = timezone.now() - timedelta(days=7)
    active_students = CustomUser.objects.filter(
        role='student', 
        is_blocked=False,
        last_activity__gte=week_ago
    ).count()
    total_students = CustomUser.objects.filter(role='student', is_blocked=False).count()
    
    # Körilgan darslar soni
    total_video_views = VideoProgress.objects.filter(watched=True).count()
    total_test_attempts = TestResult.first_attempts().count()
    total_homework_submissions = HomeworkSubmission.objects.count()
    
    # Örtaça test bali
    avg_test_score = TestResult.first_attempts().aggregate(avg=Avg('score'))['avg'] or 0
    
    # Tugallangan darslar foizi
    total_possible_views = CourseEnrollment.objects.count() * total_video_lessons if total_video_lessons > 0 else 1
    completion_percent = (total_video_views / total_possible_views * 100) if total_possible_views > 0 else 0
    
    # ==================== KUNLIK FAOLLIK (dinamik davr) ====================
    # Filter bölsa - şu davr uçun, aks holda oxirgi 30 kun
    if date_from and date_to:
        chart_start = date_from
        chart_end = date_to
    else:
        chart_end = today
        chart_start = today - timedelta(days=29)

    chart_days = (chart_end - chart_start).days + 1
    # Agar davr juda uzun bölsa (>120 kun), oy böyiça guruhla
    daily_activity = []
    activity_period_label = ""

    if chart_days > 120:
        # Oylar böyiça guruhlaş
        activity_period_label = f"{chart_start.strftime('%d.%m.%Y')} - {chart_end.strftime('%d.%m.%Y')} (oylar böyiça)"
        cursor = date(chart_start.year, chart_start.month, 1)
        end_cursor = date(chart_end.year, chart_end.month, 1)
        while cursor <= end_cursor:
            if cursor.month == 12:
                next_cursor = date(cursor.year + 1, 1, 1)
            else:
                next_cursor = date(cursor.year, cursor.month + 1, 1)
            month_end = next_cursor - timedelta(days=1)
            m_start_dt = timezone.make_aware(timezone.datetime.combine(cursor, timezone.datetime.min.time()))
            m_end_dt = timezone.make_aware(timezone.datetime.combine(month_end, timezone.datetime.max.time()))

            videos_watched = VideoProgress.objects.filter(watched_at__range=(m_start_dt, m_end_dt)).count()
            tests_taken = TestResult.objects.filter(completed_at__range=(m_start_dt, m_end_dt)).count()
            homeworks_sent = HomeworkSubmission.objects.filter(submitted_at__range=(m_start_dt, m_end_dt)).count()

            month_names = ['Yan', 'Fev', 'Mar', 'Apr', 'May', 'Iyn', 'Iyl', 'Avg', 'Sen', 'Okt', 'Noy', 'Dek']
            daily_activity.append({
                'date': f"{month_names[cursor.month - 1]} {cursor.year}",
                'videos': videos_watched,
                'tests': tests_taken,
                'homeworks': homeworks_sent,
                'total': videos_watched + tests_taken + homeworks_sent,
            })
            cursor = next_cursor
    else:
        activity_period_label = f"{chart_start.strftime('%d.%m.%Y')} - {chart_end.strftime('%d.%m.%Y')}"
        for i in range(chart_days):
            day = chart_start + timedelta(days=i)
            day_start = timezone.make_aware(timezone.datetime.combine(day, timezone.datetime.min.time()))
            day_end = timezone.make_aware(timezone.datetime.combine(day, timezone.datetime.max.time()))

            videos_watched = VideoProgress.objects.filter(watched_at__range=(day_start, day_end)).count()
            tests_taken = TestResult.objects.filter(completed_at__range=(day_start, day_end)).count()
            homeworks_sent = HomeworkSubmission.objects.filter(submitted_at__range=(day_start, day_end)).count()

            daily_activity.append({
                'date': day.strftime('%d.%m'),
                'videos': videos_watched,
                'tests': tests_taken,
                'homeworks': homeworks_sent,
                'total': videos_watched + tests_taken + homeworks_sent
            })
    
    # ==================== BARÇA DARSLAR ====================
    lessons = Lesson.objects.select_related('profession', 'section').all()
    if profession_id:
        lessons = lessons.filter(profession_id=profession_id)
    if search_query:
        lessons = lessons.filter(title__icontains=search_query)
    
    # ==================== VIDEO DARSLAR STATISTIKASI ====================
    video_lessons = lessons.filter(lesson_type='video')
    video_stats = []
    for lesson in video_lessons:
        try:
            video = lesson.video
            total_enrolled = CourseEnrollment.objects.filter(profession=lesson.profession).count()
            
            video_progress_qs = VideoProgress.objects.filter(video=video)
            if date_from:
                video_progress_qs = video_progress_qs.filter(watched_at__date__gte=date_from)
            if date_to:
                video_progress_qs = video_progress_qs.filter(watched_at__date__lte=date_to)
            
            watched_count = video_progress_qs.filter(watched=True).count()
            total_views = video_progress_qs.count()
            not_watched = total_enrolled - watched_count
            completion_rate = (watched_count / total_enrolled * 100) if total_enrolled > 0 else 0
            
            # Kim körmagan
            watched_user_ids = video_progress_qs.filter(watched=True).values_list('user_id', flat=True)
            not_watched_users = CustomUser.objects.filter(
                enrollments__profession=lesson.profession
            ).exclude(pk__in=watched_user_ids)[:5]
            
            video_stats.append({
                'lesson': lesson,
                'total_enrolled': total_enrolled,
                'total_views': total_views,
                'watched': watched_count,
                'not_watched': not_watched,
                'completion_rate': round(completion_rate, 1),
                'not_watched_users': not_watched_users,
            })
        except:
            pass
    
    video_stats_sorted = sorted(video_stats, key=lambda x: x['completion_rate'])
    
    # ==================== TEST STATISTIKASI ====================
    test_lessons = lessons.filter(lesson_type='test')
    test_stats = []
    hardest_questions = []
    
    for lesson in test_lessons:
        try:
            test = lesson.test
            total_enrolled = CourseEnrollment.objects.filter(profession=lesson.profession).count()
            
            results_qs = TestResult.objects.filter(test=test)
            if date_from:
                results_qs = results_qs.filter(completed_at__date__gte=date_from)
            if date_to:
                results_qs = results_qs.filter(completed_at__date__lte=date_to)
            
            completed_count = results_qs.values('student').distinct().count()
            total_attempts = results_qs.count()
            not_completed = total_enrolled - completed_count
            completion_rate = (completed_count / total_enrolled * 100) if total_enrolled > 0 else 0
            
            avg_score = results_qs.aggregate(avg=Avg('score'))['avg'] or 0
            passed_count = results_qs.filter(passed=True).count()
            failed_count = results_qs.filter(passed=False).count()
            pass_rate = (passed_count / total_attempts * 100) if total_attempts > 0 else 0
            
            # Qayta topşirganlar
            retry_students = results_qs.values('student').annotate(
                attempt_count=Count('id')
            ).filter(attempt_count__gt=1).count()
            
            # Kim yeçmagan
            completed_user_ids = results_qs.values_list('student_id', flat=True)
            not_completed_users = CustomUser.objects.filter(
                enrollments__profession=lesson.profession
            ).exclude(pk__in=completed_user_ids)[:5]
            
            # Kim öta olmadi
            failed_users = CustomUser.objects.filter(
                test_results__test=test,
                test_results__passed=False
            ).distinct()[:5]
            
            test_stats.append({
                'lesson': lesson,
                'test': test,
                'total_enrolled': total_enrolled,
                'completed': completed_count,
                'total_attempts': total_attempts,
                'not_completed': not_completed,
                'completion_rate': round(completion_rate, 1),
                'avg_score': round(avg_score, 1),
                'passed_count': passed_count,
                'failed_count': failed_count,
                'pass_rate': round(pass_rate, 1),
                'retry_students': retry_students,
                'not_completed_users': not_completed_users,
                'failed_users': failed_users,
            })
            
            # Eng qiyin savollar
            for question in test.questions.all():
                total_answered = TestUserAnswer.objects.filter(question=question).count()
                wrong_answers = TestUserAnswer.objects.filter(question=question, is_correct=False).count()
                if total_answered > 0:
                    error_rate = (wrong_answers / total_answered * 100)
                    if error_rate > 40:
                        hardest_questions.append({
                            'question': question,
                            'test': test,
                            'total_answered': total_answered,
                            'wrong_count': wrong_answers,
                            'error_rate': round(error_rate, 1),
                        })
        except:
            pass
    
    test_stats_sorted = sorted(test_stats, key=lambda x: x['avg_score'])
    hardest_questions = sorted(hardest_questions, key=lambda x: x['error_rate'], reverse=True)[:15]
    
    # ==================== UY VAZIFASI STATISTIKASI ====================
    homework_lessons = lessons.filter(lesson_type='homework')
    homework_stats = []
    for lesson in homework_lessons:
        try:
            homework = lesson.homework
            total_enrolled = CourseEnrollment.objects.filter(profession=lesson.profession).count()
            
            submissions_qs = HomeworkSubmission.objects.filter(homework=homework)
            if date_from:
                submissions_qs = submissions_qs.filter(submitted_at__date__gte=date_from)
            if date_to:
                submissions_qs = submissions_qs.filter(submitted_at__date__lte=date_to)
            
            submitted_count = submissions_qs.values('student').distinct().count()
            not_submitted = total_enrolled - submitted_count
            completion_rate = (submitted_count / total_enrolled * 100) if total_enrolled > 0 else 0
            
            avg_grade = submissions_qs.filter(grade__isnull=False).aggregate(avg=Avg('grade'))['avg'] or 0
            pending_count = submissions_qs.filter(status='pending').count()
            graded_count = submissions_qs.filter(status='graded').count()
            
            # Kim topşirmagan
            submitted_user_ids = submissions_qs.values_list('student_id', flat=True)
            not_submitted_users = CustomUser.objects.filter(
                enrollments__profession=lesson.profession
            ).exclude(pk__in=submitted_user_ids)[:5]
            
            homework_stats.append({
                'lesson': lesson,
                'homework': homework,
                'total_enrolled': total_enrolled,
                'submitted': submitted_count,
                'not_submitted': not_submitted,
                'completion_rate': round(completion_rate, 1),
                'avg_grade': round(avg_grade, 1),
                'pending_count': pending_count,
                'graded_count': graded_count,
                'not_submitted_users': not_submitted_users,
            })
        except:
            pass
    
    homework_stats_sorted = sorted(homework_stats, key=lambda x: x['completion_rate'])
    
    # ==================== ÖQUVÇILAR FAOLLIGI ====================
    # Passiv öquvçilar - oxirgi 7 kunda faollik körsatmaganlar
    passive_students = CustomUser.objects.filter(
        role='student',
        is_blocked=False
    ).filter(
        Q(last_activity__lt=week_ago) | Q(last_activity__isnull=True)
    ).order_by('last_activity')[:20]
    
    # Qiynalayotgan öquvçilar - testlardan past ball olganlar
    struggling_students = CustomUser.objects.filter(
        role='student',
        test_results__score__lt=50
    ).annotate(
        low_score_count=Count('test_results', filter=Q(test_results__score__lt=50)),
        avg_score=Avg('test_results__score')
    ).filter(low_score_count__gte=2).order_by('-low_score_count')[:15]
    
    # ==================== TOP DARSLAR ====================
    top_video_lessons = sorted(video_stats, key=lambda x: x['watched'], reverse=True)[:10]
    top_test_lessons = sorted(test_stats, key=lambda x: x['total_attempts'], reverse=True)[:10]
    
    # ==================== SERTIFIKATGA YAQIN ÖQUVÇILAR ====================
    near_certificate_students = []
    for profession in Profession.objects.all():
        total_lessons_in_course = profession.lessons.exclude(lesson_type='dictionary').count()
        if total_lessons_in_course == 0:
            continue
            
        enrolled_students = CustomUser.objects.filter(enrollments__profession=profession)
        for student in enrolled_students:
            # Video progress
            videos_watched = VideoProgress.objects.filter(
                user=student,
                video__lesson__profession=profession,
                watched=True
            ).count()
            # Tests passed
            tests_passed = TestResult.objects.filter(
                student=student,
                test__lesson__profession=profession,
                passed=True
            ).values('test').distinct().count()
            # Homework graded
            homework_done = HomeworkSubmission.objects.filter(
                student=student,
                homework__lesson__profession=profession,
                status='graded'
            ).values('homework').distinct().count()
            
            total_done = videos_watched + tests_passed + homework_done
            progress = (total_done / total_lessons_in_course * 100) if total_lessons_in_course > 0 else 0
            
            if 70 <= progress < 100:
                near_certificate_students.append({
                    'student': student,
                    'profession': profession,
                    'progress': round(progress, 1),
                    'remaining': total_lessons_in_course - total_done,
                })
    
    near_certificate_students = sorted(near_certificate_students, key=lambda x: x['progress'], reverse=True)[:10]
    
    # ==================== SMART ALERTS ====================
    alerts = []
    
    # 7 kun kirmagan öquvçilar
    inactive_count = passive_students.count()
    if inactive_count > 0:
        alerts.append({
            'type': 'warning',
            'icon': 'bi-clock-history',
            'title': f'{inactive_count} ta öquvçi 7+ kun kirmagan',
            'description': 'Bu öquvçilarga eslatma yuborişni öylab köring.',
        })
    
    # Testdan öta olmayotganlar
    struggling_count = struggling_students.count()
    if struggling_count > 0:
        alerts.append({
            'type': 'danger',
            'icon': 'bi-exclamation-triangle',
            'title': f'{struggling_count} ta öquvçi testlardan qiynalmoqda',
            'description': 'Bu öquvçilarga qöşimça yordam kerak bölişi mumkin.',
        })
    
    # Past tugallaş darajasidagi darslar
    low_completion_lessons = [v for v in video_stats if v['completion_rate'] < 30]
    if len(low_completion_lessons) > 0:
        alerts.append({
            'type': 'info',
            'icon': 'bi-camera-video-off',
            'title': f'{len(low_completion_lessons)} ta video dars kam körilmoqda',
            'description': 'Bu darslar kontentini tekşirib köring.',
        })
    
    # Tekşirilmagan uy vazifalari
    pending_hw_count = HomeworkSubmission.objects.filter(status='pending').count()
    if pending_hw_count > 0:
        alerts.append({
            'type': 'primary',
            'icon': 'bi-journal-text',
            'title': f'{pending_hw_count} ta uy vazifasi tekşirilmagan',
            'description': 'Uy vazifalarini baholaş kutilmoqda.',
        })
    
    professions = Profession.objects.all()

    # Yil röyxati: eng eski yozilish/dars sanasidan hozirgi yilgaça
    from django.db.models import Min as _Min
    first_enrollment = CourseEnrollment.objects.aggregate(min=_Min('enrolled_at'))['min']
    if first_enrollment:
        start_year = first_enrollment.year
    else:
        start_year = today.year - 2
    year_choices = list(range(today.year, start_year - 1, -1))
    month_choices = [
        (1, 'Yanvar'), (2, 'Fevral'), (3, 'Mart'), (4, 'Aprel'),
        (5, 'May'), (6, 'Iyun'), (7, 'Iyul'), (8, 'Avgust'),
        (9, 'Sentabr'), (10, 'Oktabr'), (11, 'Noyabr'), (12, 'Dekabr'),
    ]

    # Faol filterlar röyxati (chip'lar uçun)
    def _remove_url(remove_key):
        """Berilgan parametrni olib taşlab, qolgan filterlar bilan URL yasaydi."""
        params = request.GET.copy()
        if remove_key in params:
            params.pop(remove_key)
        qs = params.urlencode()
        return '?' + qs if qs else '?'

    # Quick range chips URL - boşqa filterlarni saqlaş bilan
    def _range_url(range_key):
        params = request.GET.copy()
        # date_from/date_to va year/month ni tozalaymiz, chunki range ular bilan konflikt qiladi
        for k in ('date_from', 'date_to', 'year', 'month', 'range'):
            if k in params:
                params.pop(k)
        if range_key:
            params['range'] = range_key
        qs = params.urlencode()
        return '?' + qs if qs else '?'

    range_urls = {
        'all': _range_url(''),
        'today': _range_url('today'),
        'week': _range_url('week'),
        'month': _range_url('month'),
        'year': _range_url('year'),
    }

    active_filters = []
    if search_query:
        active_filters.append({'label': f"Qidiruv: {search_query}", 'remove_url': _remove_url('q')})
    if profession_id:
        prof = professions.filter(pk=profession_id).first()
        if prof:
            active_filters.append({'label': f"Kurs: {prof.name}", 'remove_url': _remove_url('profession')})
    if year_int:
        active_filters.append({'label': f"Yil: {year_int}", 'remove_url': _remove_url('year')})
    if month_int:
        month_name = dict(month_choices).get(month_int, str(month_int))
        active_filters.append({'label': f"Oy: {month_name}", 'remove_url': _remove_url('month')})
    if quick_range:
        range_labels = {'today': 'Bugun', 'week': '7 kun', 'month': 'Şu oy', 'year': 'Şu yil'}
        active_filters.append({'label': f"Davr: {range_labels.get(quick_range, quick_range)}", 'remove_url': _remove_url('range')})
    if request.GET.get('date_from'):
        active_filters.append({'label': f"Dan: {request.GET.get('date_from')}", 'remove_url': _remove_url('date_from')})
    if request.GET.get('date_to'):
        active_filters.append({'label': f"Gaça: {request.GET.get('date_to')}", 'remove_url': _remove_url('date_to')})

    context = {
        # Umumiy statistika
        'total_lessons': total_lessons,
        'total_video_lessons': total_video_lessons,
        'total_test_lessons': total_test_lessons,
        'total_homework_lessons': total_homework_lessons,
        'active_students': active_students,
        'total_students': total_students,
        'total_video_views': total_video_views,
        'total_test_attempts': total_test_attempts,
        'total_homework_submissions': total_homework_submissions,
        'avg_test_score': round(avg_test_score, 1),
        'completion_percent': round(completion_percent, 1),

        # Kunlik faollik
        'daily_activity': daily_activity,
        'activity_period_label': activity_period_label,

        # Darslar statistikasi
        'video_stats': video_stats_sorted,
        'test_stats': test_stats_sorted,
        'homework_stats': homework_stats_sorted,
        'hardest_questions': hardest_questions,

        # Öquvçilar
        'passive_students': passive_students,
        'struggling_students': struggling_students,
        'near_certificate_students': near_certificate_students,

        # Top darslar
        'top_video_lessons': top_video_lessons,
        'top_test_lessons': top_test_lessons,

        # Alerts
        'alerts': alerts,

        # Filterlar
        'professions': professions,
        'selected_profession': profession_id,
        'date_from': date_from,
        'date_to': date_to,
        'search_query': search_query,
        'year_choices': year_choices,
        'month_choices': month_choices,
        'selected_year': year_int,
        'selected_month': month_int,
        'selected_range': quick_range,
        'active_filters': active_filters,
        'range_urls': range_urls,
    }

    return render(request, 'accounts/admin/lesson_statistics.html', context)


# ==================== QURILMALAR BOŞQARUVI ====================

@login_required
def my_devices(request):
    current_session_key = request.session.session_key
    devices = list(request.user.devices.filter(is_active=True).order_by('-last_login'))

    # Attach active sessions and find current device
    current_device = None
    current_session = None
    for device in devices:
        active_sessions = list(device.sessions.filter(is_active=True).order_by('-last_activity'))
        device.active_sessions_list = active_sessions
        for s in active_sessions:
            if s.session_key == current_session_key:
                current_device = device
                current_session = s

    return render(request, 'accounts/devices.html', {
        'devices': devices,
        'current_device': current_device,
        'current_session': current_session,
        'current_session_key': current_session_key,
    })


@login_required
def logout_session(request, pk):
    """End a specific session by UserSession pk."""
    if request.method != 'POST':
        return redirect('my_devices')
    from django.contrib.sessions.models import Session as DjangoSession
    from accounts.models import UserSession
    session = get_object_or_404(UserSession, pk=pk, user=request.user)
    current_session_key = request.session.session_key
    if session.session_key == current_session_key:
        messages.warning(request, "Joriy sessionni bu yerdan tugatib bölmaydi.")
        return redirect('my_devices')
    try:
        DjangoSession.objects.filter(session_key=session.session_key).delete()
    except Exception:
        pass
    session.is_active = False
    session.save()
    messages.success(request, "Session muvaffaqiyatli tugatildi.")
    return redirect('my_devices')


@login_required
def remove_device(request, pk):
    if request.method != 'POST':
        return redirect('my_devices')
    
    device = get_object_or_404(UserDevice, pk=pk, user=request.user)
    current_session_key = request.session.session_key
    
    # Check if it's the current device
    is_current = device.sessions.filter(session_key=current_session_key, is_active=True).exists()
    
    if is_current:
        messages.error(request, "Joriy qurilmani öçirib bölmaydi!")
        return redirect('my_devices')
    
    # Delete all sessions for this device (so user gets logged out)
    from django.contrib.sessions.models import Session
    
    # Get session keys for this device
    session_keys = list(device.sessions.values_list('session_key', flat=True))
    
    # Delete Django sessions
    Session.objects.filter(session_key__in=session_keys).delete()
    
    # Delete custom sessions
    device.sessions.all().delete()
    device.is_active = False
    device.save()
    
    messages.success(request, f"'{device.device_name}' qurilmasi öçirildi!")
    return redirect('my_devices')


@login_required
def logout_device(request, pk):
    if request.method != 'POST':
        return redirect('my_devices')
    
    device = get_object_or_404(UserDevice, pk=pk, user=request.user)
    current_session_key = request.session.session_key
    
    # Check if it's the current device
    is_current = device.sessions.filter(session_key=current_session_key, is_active=True).exists()
    
    if is_current:
        messages.error(request, "Joriy qurilmadan çiqiş uçun 'Çiqiş' tugmasini işlating!")
        return redirect('my_devices')
    
    # Delete all active sessions for this device (so user gets logged out)
    from django.contrib.sessions.models import Session
    
    # Get active session keys for this device
    active_session_keys = list(device.sessions.filter(is_active=True).values_list('session_key', flat=True))
    
    # Delete Django sessions
    Session.objects.filter(session_key__in=active_session_keys).delete()
    
    # Update custom sessions
    device.sessions.filter(is_active=True).update(is_active=False)
    
    messages.success(request, f"'{device.device_name}' qurilmasidan çiqildi!")
    return redirect('my_devices')


@login_required
def trust_device(request, pk):
    device = get_object_or_404(UserDevice, pk=pk, user=request.user)
    device.is_trusted = not device.is_trusted
    device.save()
    
    if device.is_trusted:
        messages.success(request, f"'{device.device_name}' işonçli qurilma sifatida belgilandi!")
    else:
        messages.success(request, f"'{device.device_name}' işonçli qurilmalar röyxatidan çiqarildi!")
    
    return redirect('my_devices')


@login_required
def logout_all_devices(request):
    if request.method == 'POST':
        from django.contrib.sessions.models import Session
        current_session_key = request.session.session_key
        
        sessions_to_kill = UserSession.objects.filter(
            user=request.user, is_active=True
        ).exclude(session_key=current_session_key)
        
        keys_to_kill = list(sessions_to_kill.values_list('session_key', flat=True))
        Session.objects.filter(session_key__in=keys_to_kill).delete()
        sessions_to_kill.update(is_active=False)
        
        messages.success(request, "Barça boşqa qurilmalardan çiqildi!")
    
    return redirect('my_devices')


@login_required
def remove_all_devices(request):
    """Joriy qurilmadan taşqari barça qurilmalarni öçiriş"""
    if request.method == 'POST':
        from django.contrib.sessions.models import Session
        current_session_key = request.session.session_key
        
        # Joriy qurilmani aniqlaş
        current_session = UserSession.objects.filter(
            user=request.user, session_key=current_session_key, is_active=True
        ).select_related('device').first()
        current_device = current_session.device if current_session else None
        
        # Boşqa qurilmalarni oliş
        other_devices = UserDevice.objects.filter(user=request.user, is_active=True)
        if current_device:
            other_devices = other_devices.exclude(pk=current_device.pk)
        
        # Sessiyalarni öçiriş
        for device in other_devices:
            session_keys = list(device.sessions.filter(is_active=True).values_list('session_key', flat=True))
            Session.objects.filter(session_key__in=session_keys).delete()
            device.sessions.filter(is_active=True).update(is_active=False)
        
        count = other_devices.count()
        other_devices.update(is_active=False)
        
        messages.success(request, f"{count} ta qurilma öçirildi!")
    
    return redirect('my_devices')


@login_required
def bulk_remove_devices(request):
    """Tanlangan qurilmalarni öçiriş"""
    if request.method == 'POST':
        from django.contrib.sessions.models import Session
        current_session_key = request.session.session_key
        device_ids = request.POST.getlist('device_ids')
        
        if not device_ids:
            messages.error(request, "Heç qanday qurilma tanlanmadi!")
            return redirect('my_devices')
        
        # Joriy qurilmani aniqlaş
        current_session = UserSession.objects.filter(
            user=request.user, session_key=current_session_key, is_active=True
        ).select_related('device').first()
        current_device_pk = current_session.device.pk if current_session and current_session.device else None
        
        removed = 0
        for device_id in device_ids:
            try:
                device = UserDevice.objects.get(pk=int(device_id), user=request.user, is_active=True)
            except (UserDevice.DoesNotExist, ValueError):
                continue
            
            # Joriy qurilmani öçirib bölmaydi
            if current_device_pk and device.pk == current_device_pk:
                continue
            
            session_keys = list(device.sessions.filter(is_active=True).values_list('session_key', flat=True))
            Session.objects.filter(session_key__in=session_keys).delete()
            device.sessions.filter(is_active=True).update(is_active=False)
            device.is_active = False
            device.save(update_fields=['is_active'])
            removed += 1
        
        if removed:
            messages.success(request, f"{removed} ta qurilma öçirildi!")
        else:
            messages.error(request, "Joriy qurilmani öçirib bölmaydi!")
    
    return redirect('my_devices')


# ==================== ADMIN: QURILMALAR ====================

@login_required
def admin_user_devices(request, pk):
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kiriş huquqi yöq!")
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
        messages.error(request, "Sizda bu sahifaga kiriş huquqi yöq!")
        return redirect('home')
    
    device = get_object_or_404(UserDevice, pk=device_pk, user_id=user_pk)
    device.sessions.filter(is_active=True).update(is_active=False)
    
    messages.success(request, f"'{device.device_name}' qurilmasidan çiqildi!")
    return redirect('admin_user_devices', pk=user_pk)


@login_required
def admin_logout_all_user_devices(request, pk):
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kiriş huquqi yöq!")
        return redirect('home')
    
    if request.method == 'POST':
        user = get_object_or_404(CustomUser, pk=pk)
        UserSession.objects.filter(user=user, is_active=True).update(is_active=False)
        messages.success(request, f"{user.full_name}ning barça qurilmalaridan çiqildi!")
    
    return redirect('admin_user_devices', pk=pk)


# ============ SYSTEM REPORTS ============




def generate_report_pdf(report):
    """Hisobot uçun PDF yaratiş"""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from io import BytesIO
    from django.core.files.base import ContentFile
    
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
    
    # Title
    elements.append(Paragraph(report.title, styles['Title']))
    elements.append(Paragraph(f"Davr: {report.date_from.strftime('%d.%m.%Y')} - {report.date_to.strftime('%d.%m.%Y')}", styles['Normal']))
    elements.append(Paragraph(f"Yaratilgan: {report.created_at.strftime('%d.%m.%Y %H:%M')}", styles['Normal']))
    elements.append(Spacer(1, 20))
    
    # 1. Foydalanuvçilar
    elements.append(Paragraph("1. Foydalanuvçilar statistikasi", heading_style))
    user_data = [
        ["Körsatkiç", 'Soni'],
        ['Jami foydalanuvçilar', str(report.total_users)],
        ["Öquvçilar", str(report.total_students)],
        ["Öqituvçilar", str(report.total_teachers)],
        ['Yangi foydalanuvçilar (davr)', str(report.new_users)],
    ]
    elements.append(create_table(user_data))
    elements.append(Spacer(1, 15))
    
    # 2. Kurslar
    elements.append(Paragraph("2. Kurslar va darslar", heading_style))
    course_data = [
        ["Körsatkiç", 'Soni'],
        ['Jami kurslar', str(report.total_courses)],
        ['Jami darslar', str(report.total_lessons)],
        ['Videodarslar', str(report.total_videos)],
        ['Testlar', str(report.total_tests)],
        ['Uy vazifalari', str(report.total_homeworks)],
    ]
    elements.append(create_table(course_data))
    elements.append(Spacer(1, 15))
    
    # 3. Davr faoliyati
    elements.append(Paragraph("3. Davr faoliyati", heading_style))
    activity_data = [
        ["Körsatkiç", 'Soni'],
        ['Yangi yozilişlar', str(report.new_enrollments)],
        ['Test topşiruvlar', str(report.test_submissions)],
        ['Vazifa topşiruvlar', str(report.homework_submissions)],
        ["Video körişlar", str(report.video_views)],
        ['Jami harakatlar', str(report.total_activities)],
    ]
    elements.append(create_table(activity_data))
    elements.append(Spacer(1, 15))
    
    # 4. Coinlar
    elements.append(Paragraph("4. Coin statistikasi", heading_style))
    coin_data = [
        ["Körsatkiç", 'Qiymat'],
        ['Jami coinlar (hozir)', str(report.total_coins)],
        ['Işlangan coinlar (davr)', str(report.coins_earned)],
        ['Sarflangan coinlar (davr)', str(report.coins_spent)],
    ]
    elements.append(create_table(coin_data))
    elements.append(Spacer(1, 15))
    
    # 5. Sertifikatlar
    elements.append(Paragraph("5. Sertifikatlar", heading_style))
    cert_data = [
        ["Körsatkiç", 'Soni'],
        ['Jami sertifikatlar', str(report.total_certificates)],
        ['Yangi sertifikatlar (davr)', str(report.new_certificates)],
    ]
    elements.append(create_table(cert_data))
    
    doc.build(elements)
    
    # PDF faylni saqlaş
    pdf_content = buffer.getvalue()
    buffer.close()
    
    filename = f"report_{report.report_type}_{report.date_from}_{report.date_to}.pdf"
    report.pdf_file.save(filename, ContentFile(pdf_content))
    report.save()








# ============ KURSGA YOZILIŞ SÖROVLARI ============

@login_required
def admin_enrollment_requests(request):
    """Admin: Kursga yoziliş sörovlarini köriş"""
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kiriş huquqi yöq!")
        return redirect('home')
    
    status_filter = request.GET.get('status', 'pending')
    profession_filter = request.GET.get('profession')
    search = request.GET.get('q')
    
    tab = request.GET.get('tab', 'enroll')  # 'enroll' yoki 'unenroll'
    
    requests_qs = CourseEnrollmentRequest.objects.select_related('user', 'profession')
    
    if status_filter:
        requests_qs = requests_qs.filter(status=status_filter)
    
    if profession_filter:
        requests_qs = requests_qs.filter(profession_id=profession_filter)
    
    if search:
        requests_qs = requests_qs.filter(
            Q(user__first_name__icontains=search) |
            Q(user__last_name__icontains=search) |
            Q(user__phone__icontains=search)
        )
    
    # Kursdan çiqiş sörovlari
    unenroll_qs = CourseUnenrollRequest.objects.select_related('user', 'profession')
    if status_filter:
        unenroll_qs = unenroll_qs.filter(status=status_filter)
    if profession_filter:
        unenroll_qs = unenroll_qs.filter(profession_id=profession_filter)
    if search:
        unenroll_qs = unenroll_qs.filter(
            Q(user__first_name__icontains=search) |
            Q(user__last_name__icontains=search) |
            Q(user__phone__icontains=search)
        )
    
    professions = Profession.objects.all()
    pending_count = CourseEnrollmentRequest.objects.filter(status='pending').count()
    approved_count = CourseEnrollmentRequest.objects.filter(status='approved').count()
    rejected_count = CourseEnrollmentRequest.objects.filter(status='rejected').count()
    
    unenroll_pending = CourseUnenrollRequest.objects.filter(status='pending').count()
    unenroll_approved = CourseUnenrollRequest.objects.filter(status='approved').count()
    unenroll_rejected = CourseUnenrollRequest.objects.filter(status='rejected').count()
    
    from .study_groups import attach_group_options

    context = {
        'enrollment_requests': attach_group_options(list(requests_qs), request.user),
        'unenroll_requests': unenroll_qs,
        'professions': professions,
        'status_filter': status_filter,
        'profession_filter': profession_filter,
        'search': search,
        'tab': tab,
        'pending_count': pending_count,
        'approved_count': approved_count,
        'rejected_count': rejected_count,
        'unenroll_pending': unenroll_pending,
        'unenroll_approved': unenroll_approved,
        'unenroll_rejected': unenroll_rejected,
    }
    return render(request, 'accounts/admin/enrollment_requests.html', context)


@login_required
def approve_enrollment_request(request, pk):
    """Admin: Sörovni qabul qiliş"""
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kiriş huquqi yöq!")
        return redirect('home')
    
    enrollment_request = get_object_or_404(CourseEnrollmentRequest, pk=pk)
    
    if request.method == 'POST':
        from .course_membership import approve_enrollment
        from .study_groups import resolve_group
        group = resolve_group(request.user, enrollment_request.profession, request.POST.get('group'))
        if group is None:
            messages.error(request, "Öquvçini qaysi guruhga qöşişni tanlang. Guruhsiz öquvçi kursga qöşilmaydi.")
        elif approve_enrollment(enrollment_request, request.user, group):
            messages.success(request, f"{enrollment_request.user.full_name} - {enrollment_request.profession.name} kursiga qabul qilindi va «{group.name}» guruhiga qöşildi!")
        else:
            messages.info(request, "Bu sörov allaqaçon körib çiqilgan.")
    
    return redirect('admin_enrollment_requests')


@login_required
def reject_enrollment_request(request, pk):
    """Admin: Sörovni rad etiş"""
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kiriş huquqi yöq!")
        return redirect('home')
    
    enrollment_request = get_object_or_404(CourseEnrollmentRequest, pk=pk)
    
    if request.method == 'POST':
        from .course_membership import reject_enrollment
        admin_message = request.POST.get('admin_message', '').strip()
        if reject_enrollment(enrollment_request, request.user, admin_message):
            messages.warning(request, f"{enrollment_request.user.full_name} - {enrollment_request.profession.name} sörovi rad etildi!")
        else:
            messages.info(request, "Bu sörov allaqaçon körib çiqilgan.")
    
    return redirect('admin_enrollment_requests')


@login_required
def admin_unenroll_requests(request):
    """Admin: Kursdan çiqiş sörovlari"""
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kiriş huquqi yöq!")
        return redirect('home')

    status_filter = request.GET.get('status', 'pending')
    requests_qs = CourseUnenrollRequest.objects.select_related('user', 'profession')

    if status_filter:
        requests_qs = requests_qs.filter(status=status_filter)

    pending_count = CourseUnenrollRequest.objects.filter(status='pending').count()
    approved_count = CourseUnenrollRequest.objects.filter(status='approved').count()
    rejected_count = CourseUnenrollRequest.objects.filter(status='rejected').count()

    context = {
        'unenroll_requests': requests_qs,
        'status_filter': status_filter,
        'pending_count': pending_count,
        'approved_count': approved_count,
        'rejected_count': rejected_count,
    }
    return render(request, 'accounts/admin/unenroll_requests.html', context)


@login_required
def approve_unenroll_request(request, pk):
    """Admin: Kursdan çiqiş sörovini tasdiqlaş"""
    if not request.user.is_admin:
        return redirect('home')

    unenroll_req = get_object_or_404(CourseUnenrollRequest, pk=pk)

    if request.method == 'POST':
        from .course_membership import approve_unenroll
        total_coins_earned = approve_unenroll(unenroll_req, request.user)
        if total_coins_earned is None:
            messages.info(request, "Bu sörov allaqaçon körib çiqilgan.")
        else:
            coin_msg = f" ({total_coins_earned} coin ayirildi)" if total_coins_earned > 0 else ""
            messages.success(request, f"{unenroll_req.user.full_name} - {unenroll_req.profession.name} kursidan çiqarildi!{coin_msg}")

    return redirect('admin_unenroll_requests')


@login_required
def reject_unenroll_request(request, pk):
    """Admin: Kursdan çiqiş sörovini rad etiş"""
    if not request.user.is_admin:
        return redirect('home')

    unenroll_req = get_object_or_404(CourseUnenrollRequest, pk=pk)

    if request.method == 'POST':
        from .course_membership import reject_unenroll
        admin_message = request.POST.get('admin_message', '').strip()
        if reject_unenroll(unenroll_req, request.user, admin_message):
            messages.warning(request, f"{unenroll_req.user.full_name} - çiqiş sörovi rad etildi!")
        else:
            messages.info(request, "Bu sörov allaqaçon körib çiqilgan.")

    return redirect('admin_unenroll_requests')


def password_reset_request_view(request):
    from .password_recovery import email_password_reset
    return email_password_reset(request)








@login_required
def admin_parents(request):
    if not request.user.is_admin:
        return redirect('home')
    
    parents = Parent.objects.all()
    
    q = request.GET.get('q', '')
    status = request.GET.get('status', '')
    
    if q:
        parents = parents.filter(
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q) |
            Q(phone__icontains=q) |
            Q(username__icontains=q) |
            Q(email__icontains=q)
        )
    
    if status == 'active':
        parents = parents.filter(is_active=True)
    elif status == 'inactive':
        parents = parents.filter(is_active=False)
    
    context = {
        'parents': parents,
        'q': q,
        'status': status,
    }
    
    return render(request, 'accounts/admin/parents.html', context)


@login_required
def admin_parent_add(request):
    if not request.user.is_admin:
        return redirect('home')
    
    students = CustomUser.objects.filter(role='student')
    
    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        phone = request.POST.get('phone', '').strip()
        email = request.POST.get('email', '').strip() or None
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        children_ids = request.POST.getlist('children')

        # Ilgari heç qanday parol siyosati yöq edi — admin "123" ham qöya olardi.
        policy_error = validate_parent_password(password)

        if Parent.objects.filter(username=username).exists():
            messages.error(request, "Bu username allaqaçon mavjud!")
        elif Parent.objects.filter(phone=phone).exists():
            messages.error(request, "Bu telefon raqam allaqaçon mavjud!")
        elif policy_error:
            messages.error(request, policy_error)
        else:
            parent = Parent.objects.create(
                first_name=first_name,
                last_name=last_name,
                phone=phone,
                email=email,
                username=username,
                password=make_password(password)
            )
            
            if children_ids:
                parent.children.set(children_ids)
            
            messages.success(request, "Ota-ona muvaffaqiyatli qöşildi!")
            return redirect('admin_parents')
    
    context = {
        'children': students,
        'selected_children': [],
        'title': "Ota-ona qöşiş",
    }
    
    return render(request, 'accounts/admin/parent_form.html', context)


@login_required
def admin_parent_edit(request, pk):
    if not request.user.is_admin:
        return redirect('home')
    
    parent = get_object_or_404(Parent, pk=pk)
    students = CustomUser.objects.filter(role='student')
    
    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        phone = request.POST.get('phone', '').strip()
        email = request.POST.get('email', '').strip() or None
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        children_ids = request.POST.getlist('children')

        # Böş parol = "özgartirmang", şuning uçun faqat berilganda tekşiriladi.
        policy_error = validate_parent_password(password) if password else ''

        if Parent.objects.filter(username=username).exclude(pk=pk).exists():
            messages.error(request, "Bu username allaqaçon mavjud!")
        elif Parent.objects.filter(phone=phone).exclude(pk=pk).exists():
            messages.error(request, "Bu telefon raqam allaqaçon mavjud!")
        elif policy_error:
            messages.error(request, policy_error)
        else:
            parent.first_name = first_name
            parent.last_name = last_name
            parent.phone = phone
            parent.email = email
            parent.username = username
            
            if password:
                parent.password = make_password(password)
            
            parent.save()
            parent.children.set(children_ids)
            
            messages.success(request, "Ota-ona muvaffaqiyatli tahrirlandi!")
            return redirect('admin_parents')
    
    context = {
        'parent': parent,
        'children': students,
        'selected_children': list(parent.children.values_list('id', flat=True)),
        'title': "Ota-onani tahrirlaş",
    }
    
    return render(request, 'accounts/admin/parent_form.html', context)


@login_required
def admin_parent_view(request, pk):
    if not request.user.is_admin:
        return redirect('home')
    
    parent = get_object_or_404(Parent, pk=pk)
    children = parent.children.all()
    parent_messages = ParentMessage.objects.filter(parent=parent).order_by('-created_at')
    
    children_data = []
    for child in children:
        enrollments = child.enrollments.all().select_related('profession')
        
        child_info = {
            'user': child,
            'enrollments': [],
        }
        
        for enrollment in enrollments:
            profession = enrollment.profession
            
            total_videos = VideoLesson.objects.filter(lesson__profession=profession).count()
            watched_videos = VideoProgress.objects.filter(
                user=child,
                video__lesson__profession=profession,
                watched=True
            ).count()
            
            test_results = TestResult.objects.filter(
                student=child,
                test__lesson__profession=profession
            )
            tests_taken = test_results.count()
            avg_test_score = test_results.aggregate(avg=Avg('score'))['avg'] or 0
            
            homework_submissions = HomeworkSubmission.objects.filter(
                student=child,
                homework__lesson__profession=profession
            )
            total_homeworks = Homework.objects.filter(lesson__profession=profession).count()
            submitted_homeworks = homework_submissions.count()
            avg_homework_grade = homework_submissions.filter(grade__isnull=False).aggregate(avg=Avg('grade'))['avg'] or 0
            
            total_items = total_videos + Test.objects.filter(lesson__profession=profession).count() + total_homeworks
            completed_items = watched_videos + tests_taken + submitted_homeworks
            overall_progress = (completed_items / total_items * 100) if total_items > 0 else 0
            
            child_info['enrollments'].append({
                'profession': profession,
                'overall_progress': round(overall_progress, 1),
                'watched_videos': watched_videos,
                'total_videos': total_videos,
                'tests_taken': tests_taken,
                'avg_test_score': round(avg_test_score, 1),
                'submitted_homeworks': submitted_homeworks,
                'total_homeworks': total_homeworks,
                'avg_homework_grade': round(avg_homework_grade, 1),
            })
        
        children_data.append(child_info)
    
    context = {
        'parent': parent,
        'children_data': children_data,
        'parent_messages': parent_messages,
    }
    
    return render(request, 'accounts/admin/parent_view.html', context)


@login_required
def admin_parent_toggle(request, pk):
    if not request.user.is_admin:
        return redirect('home')
    
    if request.method == 'POST':
        parent = get_object_or_404(Parent, pk=pk)
        parent.is_active = not parent.is_active
        parent.save()
        
        status_text = "faollaştirildi" if parent.is_active else "öçirildi"
        messages.success(request, f"{parent.full_name} {status_text}!")
    
    return redirect('admin_parents')


@login_required
def admin_parent_delete(request, pk):
    if not request.user.is_admin:
        return redirect('home')
    
    parent = get_object_or_404(Parent, pk=pk)
    
    if request.method == 'POST':
        parent_name = parent.full_name
        parent.delete()
        messages.success(request, f"{parent_name} muvaffaqiyatli öçirildi!")
        return redirect('admin_parents')
    
    return redirect('admin_parents')


# ============ KITOBXONA / BOOK RENTAL ============

from .models import Book, BookRental, BookRentalRequest


@login_required
def admin_book_requests(request):
    """Admin: kitob ijarasi arizalari sahifasi (API orqali)"""
    if not (request.user.is_admin or request.user.role == 'assistant_admin'):
        return redirect('home')
    return render(request, 'accounts/admin/book_requests.html')


@login_required
def admin_books(request):
    """Kitoblar röyxati"""
    if not (request.user.is_admin or request.user.is_teacher or request.user.role == 'assistant_admin'):
        return redirect('home')
    
    books = Book.objects.all()
    
    # Qidiruv
    search = request.GET.get('search', '')
    if search:
        books = books.filter(
            Q(title__icontains=search) | Q(author__icontains=search)
        )
    
    # Saralaş
    sort = request.GET.get('sort', 'title')
    sort_map = {
        'title': 'title',
        '-title': '-title',
        'author': 'author',
        '-author': '-author',
        'available': '-available_copies',
        '-available': 'available_copies',
        'newest': '-created_at',
        'oldest': 'created_at',
    }
    books = books.order_by(sort_map.get(sort, 'title'))
    
    # Statistikalar
    total_books = Book.objects.count()
    total_copies = sum(b.total_copies for b in Book.objects.all())
    available_total = sum(b.available_copies for b in Book.objects.all())
    
    context = {
        'books': books,
        'search': search,
        'sort': sort,
        'total_books': total_books,
        'total_copies': total_copies,
        'available_total': available_total,
    }
    return render(request, 'accounts/admin/books.html', context)


@login_required
@require_GET
def book_check_duplicate(request):
    """Kitob duplicate tekşiruvi — oddiy Django JSON view (DRF yoki token kerak emas)."""
    from django.http import JsonResponse
    from django.db.models import Q as _DQ

    title      = request.GET.get('title', '').strip()
    author     = request.GET.get('author', '').strip()
    isbn       = request.GET.get('isbn', '').strip()
    exclude_id = request.GET.get('exclude_id', '').strip()  # tahrirlaşda özini çiqarib taşlaydi

    empty = JsonResponse({'has_exact_duplicate': False, 'duplicate_reason': None, 'exact': None, 'similar_books': []})
    if not title and not isbn:
        return empty

    # exclude_id ni int ga aylantiramiz
    exclude_pk = None
    if exclude_id.isdigit():
        exclude_pk = int(exclude_id)

    def bdict(b):
        return {
            'id': b.pk,
            'title': b.title,
            'author': b.author,
            'isbn': b.isbn or '',
            'total_copies': b.total_copies,
            'available_copies': b.available_copies,
        }

    def base_qs():
        qs = Book.objects.all()
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
        return qs

    # Exact: ISBN ustuvori, söng nom+muallif
    isbn_exact  = base_qs().filter(isbn__iexact=isbn).first() if isbn else None
    title_exact = base_qs().filter(title__iexact=title, author__iexact=author).first() if (title and author) else None
    primary = isbn_exact or title_exact
    duplicate_reason = 'isbn' if isbn_exact else ('title_author' if title_exact else None)

    # Öxşaş kitoblar (icontains, title YOKI author)
    similar_list = []
    if title or author:
        q = _DQ()
        if title:
            q |= _DQ(title__icontains=title)
        if author:
            q |= _DQ(author__icontains=author)
        qs = base_qs().filter(q)
        if primary:
            qs = qs.exclude(pk=primary.pk)
        similar_list = [bdict(b) for b in qs[:8]]

    return JsonResponse({
        'has_exact_duplicate': primary is not None,
        'duplicate_reason': duplicate_reason,
        'exact': bdict(primary) if primary else None,
        'similar_books': similar_list,
    })


@login_required
def admin_book_add(request):
    """Yangi kitob qöşiş"""
    if not (request.user.is_admin or request.user.is_teacher or request.user.role == 'assistant_admin'):
        return redirect('home')
    
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        author = request.POST.get('author', '').strip()
        isbn = request.POST.get('isbn', '').strip()
        description = request.POST.get('description', '')
        total_copies = int(request.POST.get('total_copies', 1))
        cover_image = request.FILES.get('cover_image')

        # Duplicate tekşiruv: bir xil nom+muallif YOKI bir xil ISBN
        from django.db.models import Q as _Q2
        dup_q = _Q2(title__iexact=title, author__iexact=author)
        if isbn:
            dup_q |= _Q2(isbn__iexact=isbn)
        existing = Book.objects.filter(dup_q).first()
        if existing:
            if isbn and existing.isbn and existing.isbn.lower() == isbn.lower():
                reason = f"ISBN «{isbn}» allaqaçon mavjud"
            else:
                reason = f"Nom va muallif bir xil"
            messages.error(
                request,
                f"Bu kitob allaqaçon kutubxonada bor! ({reason}): «{existing.title}» — {existing.author}"
            )
            return render(request, 'accounts/admin/book_form.html', {
                'action': 'add',
                'duplicate': existing,
            })

        book = Book.objects.create(
            title=title,
            author=author,
            isbn=isbn,
            description=description,
            total_copies=total_copies,
            available_copies=total_copies,
            cover_image=cover_image,
        )

        messages.success(request, f"'{book.title}' kitobi muvaffaqiyatli qöşildi!")
        return redirect('admin_books')
    
    return render(request, 'accounts/admin/book_form.html', {'action': 'add'})


@login_required
def admin_book_edit(request, pk):
    """Kitobni tahrirlaş"""
    if not (request.user.is_admin or request.user.is_teacher or request.user.role == 'assistant_admin'):
        return redirect('home')
    
    book = get_object_or_404(Book, pk=pk)
    
    if request.method == 'POST':
        book.title = request.POST.get('title')
        book.author = request.POST.get('author')
        book.isbn = request.POST.get('isbn', '')
        book.description = request.POST.get('description', '')
        new_total = int(request.POST.get('total_copies', 1))
        
        # Mavjud nusxalarni yangilaş
        difference = new_total - book.total_copies
        book.total_copies = new_total
        book.available_copies = max(0, book.available_copies + difference)
        
        if request.FILES.get('cover_image'):
            book.cover_image = request.FILES.get('cover_image')
        
        book.save()
        messages.success(request, f"'{book.title}' kitobi yangilandi!")
        return redirect('admin_books')
    
    context = {
        'book': book,
        'action': 'edit'
    }
    return render(request, 'accounts/admin/book_form.html', context)


@login_required
def admin_book_delete(request, pk):
    """Kitobni öçiriş"""
    if not (request.user.is_admin or request.user.role == 'assistant_admin'):
        return redirect('home')
    
    book = get_object_or_404(Book, pk=pk)
    
    if request.method == 'POST':
        title = book.title
        book.delete()
        messages.success(request, f"'{title}' kitobi öçirildi!")
    
    return redirect('admin_books')


@login_required
def admin_book_rentals(request):
    """Kitob ijaralari röyxati"""
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')
    
    rentals = BookRental.objects.select_related('book', 'student', 'given_by').all()
    
    # Filter
    status = request.GET.get('status', '')
    if status:
        rentals = rentals.filter(status=status)
    
    search = request.GET.get('search', '')
    if search:
        rentals = rentals.filter(
            Q(book__title__icontains=search) |
            Q(student__first_name__icontains=search) |
            Q(student__last_name__icontains=search)
        )
    
    # Saralaş
    sort = request.GET.get('sort', 'newest')
    sort_map = {
        'newest': '-created_at',
        'oldest': 'created_at',
        'book': 'book__title',
        'student': 'student__first_name',
        'return': 'return_date',
        '-return': '-return_date',
    }
    rentals = rentals.order_by(sort_map.get(sort, '-created_at'))
    
    # Statistikalar
    from datetime import date
    today = date.today()
    stats = {
        'total': BookRental.objects.filter(status='active').count(),
        'overdue': BookRental.objects.filter(status='active', return_date__lt=today).count(),
        'returned_today': BookRental.objects.filter(actual_return_date=today).count(),
    }
    
    context = {
        'rentals': rentals,
        'stats': stats,
        'status': status,
        'search': search,
        'sort': sort,
    }
    return render(request, 'accounts/admin/book_rentals.html', context)


@login_required
def admin_give_book(request):
    """Öquvçiga kitob beriş"""
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')
    
    if request.method == 'POST':
        book_id = request.POST.get('book_id')
        student_id = request.POST.get('student_id')
        return_date = request.POST.get('return_date')
        notes = request.POST.get('notes', '')
        
        book = get_object_or_404(Book, pk=book_id)
        student = get_object_or_404(CustomUser, pk=student_id, role='student')
        
        if not book.is_available:
            messages.error(request, f"'{book.title}' kitobi hozirda mavjud emas!")
            return redirect('admin_give_book')
        
        # Ijara yaratiş
        rental = BookRental.objects.create(
            book=book,
            student=student,
            given_by=request.user,
            return_date=return_date,
            notes=notes
        )
        
        # Mavjud nusxani kamaytiriş
        book.available_copies -= 1
        book.save()
        
        # Öquvçiga xabar yuboriş: "Ijaraga şu kitob berildi, qaytariş sanasi kelmoqda"
        from datetime import datetime
        return_date_str = datetime.strptime(return_date, '%Y-%m-%d').strftime('%d.%m.%Y')
        Message.objects.create(
            sender=request.user,
            recipient=student,
            title="📚 Kitob ijarasi",
            content=f"Sizga '{book.title}' ({book.author}) kitobi ijaraga berildi. Qaytariş sanasi kelmoqda: {return_date_str}\n\nIltimos, kitobni öz vaqtida kutubxonaga qaytaring!",
            message_type='system'
        )
        
        # Ota-onalarga xabar: farzandining ijaradagi kitobi va qaytariş sanasi
        for parent in student.parents.filter(is_active=True):
            ParentMessage.objects.create(
                parent=parent,
                child=student,
                subject="📚 Farzandingiz kitob oldi",
                sender_type='system',
                message=f"Farzandingiz {student.full_name} '{book.title}' ({book.author}) kitobini ijaraga oldi.\n\nQaytariş sanasi: {return_date_str}\n\nIltimos, farzandingizga kitobni öz vaqtida qaytarişni eslatib qöying!"
            )
        
        notify_admins(
            f"📚 <b>Kitob ijaraga berildi!</b>\n\n"
            f"📖 {book.title} ({book.author})\n"
            f"👤 {student.full_name}\n"
            f"📅 Qaytariş: {return_date_str}\n"
            f"👨‍💼 Berdi: {request.user.full_name}"
        )
        notify_group(
            f"📚 <b>Kutubxona yangiligi</b>\n\n"
            f"📖 «{book.title}» kitobi {student.full_name}ga ijaraga berildi.\n"
            f"📅 Qaytariş sanasi: {return_date_str}"
        )
        notify_user(
            student.telegram_id,
            f"📚 Sizga kitob ijaraga berildi!\n\n"
            f"📖 <b>{book.title}</b> ({book.author})\n"
            f"📅 Qaytariş sanasi: {return_date_str}\n\n"
            f"⏰ Iltimos, kitobni öz vaqtida qaytaring!"
        )
        messages.success(request, f"'{book.title}' kitobi {student.full_name}ga berildi!")
        return redirect('admin_book_rentals')
    
    books = Book.objects.filter(available_copies__gt=0)
    students = CustomUser.objects.filter(role='student', is_blocked=False).order_by('first_name')
    
    from datetime import date
    context = {
        'books': books,
        'students': students,
        'today': date.today(),
    }
    return render(request, 'accounts/admin/give_book.html', context)


@login_required
def admin_return_book(request, pk):
    """Kitobni qaytarilgan deb belgilaş"""
    if not (request.user.is_admin or request.user.is_teacher):
        return redirect('home')
    
    rental = get_object_or_404(BookRental, pk=pk)
    
    if request.method == 'POST':
        rental.mark_returned(request.user)
        
        # Öquvçiga xabar
        Message.objects.create(
            sender=request.user,
            recipient=rental.student,
            title="✅ Kitob qaytarildi",
            content=f"'{rental.book.title}' ({rental.book.author}) kitobi muvaffaqiyatli qaytarildi. Rahmat!",
            message_type='system'
        )
        
        notify_admins(
            f"✅ <b>Kitob qaytarildi!</b>\n\n"
            f"📖 {rental.book.title} ({rental.book.author})\n"
            f"👤 {rental.student.full_name}\n"
            f"👨‍💼 Qabul qildi: {request.user.full_name}"
        )
        notify_group(
            f"✅ <b>Kutubxona yangiligi</b>\n\n"
            f"📖 «{rental.book.title}» kitobi {rental.student.full_name} tomonidan qaytarildi."
        )
        messages.success(request, f"'{rental.book.title}' qaytarildi!")
    
    return redirect('admin_book_rentals')


# Öquvçi uçun
@login_required
def my_book_rentals(request):
    """Öquvçining ijaradagi kitoblari (eski URL — library sahifasiga yönaltiradi)"""
    from django.shortcuts import redirect
    return redirect('student_library')


@login_required
def student_library(request):
    """Öquvçilar uçun kutubxona sahifasi — API orqali işlaydi"""
    return render(request, 'accounts/library.html')


def send_book_reminders():
    """Kitob qaytariş eslatmalari - cron job yoki management command orqali çaqiriladi"""
    from datetime import date, timedelta
    
    today = date.today()
    three_days_later = today + timedelta(days=3)
    
    # 3 kun oldin eslatma
    rentals_3days = BookRental.objects.filter(
        status='active',
        return_date=three_days_later,
        reminder_sent_3days=False
    )
    
    for rental in rentals_3days:
        # Öquvçiga xabar: "Iltimos uşbu yozuvçini şu nomli kitobini kutubxonaga qaytaring"
        Message.objects.create(
            recipient=rental.student,
            title="⏰ Kitob qaytariş eslatmasi",
            content=f"Hurmatli {rental.student.full_name}!\n\n'{rental.book.title}' ({rental.book.author}) kitobini qaytariş muddati 3 kun qoldi.\n\nIltimos, uşbu {rental.book.author} yozuvçining '{rental.book.title}' nomli kitobini kutubxonaga qaytaring!\n\nQaytariş sanasi: {rental.return_date.strftime('%d.%m.%Y')}",
            message_type='system'
        )
        
        # Ota-onaga xabar: farzandining ijaradagi kitobi qaytariş sanasi
        for parent in rental.student.parents.filter(is_active=True):
            ParentMessage.objects.create(
                parent=parent,
                child=rental.student,
                subject="⏰ Kitob qaytariş eslatmasi (3 kun qoldi)",
                sender_type='system',
                message=f"Farzandingiz {rental.student.full_name} '{rental.book.title}' ({rental.book.author}) kitobini ijaraga olgan. Qaytariş sanasi: {rental.return_date.strftime('%d.%m.%Y')}\n\nIltimos, uşbu {rental.book.author} yozuvçining '{rental.book.title}' nomli kitobini kutubxonaga qaytaring deb farzandingizga aytib qöying!"
            )
        
        rental.reminder_sent_3days = True
        rental.save()
    
    # Bugungi eslatma
    rentals_today = BookRental.objects.filter(
        status='active',
        return_date=today,
        reminder_sent_today=False
    )
    
    for rental in rentals_today:
        # Öquvçiga xabar: "Iltimos uşbu yozuvçini şu nomli kitobini kutubxonaga qaytaring"
        Message.objects.create(
            recipient=rental.student,
            title="🚨 Kitobni bugun qaytaring!",
            content=f"Hurmatli {rental.student.full_name}!\n\n'{rental.book.title}' ({rental.book.author}) kitobini BUGUN qaytarişingiz kerak!\n\nIltimos, uşbu {rental.book.author} yozuvçining '{rental.book.title}' nomli kitobini kutubxonaga qaytaring!",
            message_type='system'
        )
        
        # Ota-onaga xabar: farzandining ijaradagi kitobi bugun qaytariş kerak
        for parent in rental.student.parents.filter(is_active=True):
            ParentMessage.objects.create(
                parent=parent,
                child=rental.student,
                subject="🚨 Kitobni bugun qaytaring!",
                sender_type='system',
                message=f"Farzandingiz {rental.student.full_name} '{rental.book.title}' ({rental.book.author}) kitobini BUGUN qaytarişi kerak!\n\nIltimos, uşbu {rental.book.author} yozuvçining '{rental.book.title}' nomli kitobini kutubxonaga qaytaring deb aytib qöying!"
            )
        
        rental.reminder_sent_today = True
        rental.save()
    
    # Muddati ötganlarni belgilaş
    BookRental.objects.filter(
        status='active',
        return_date__lt=today
    ).update(status='overdue')


# ============== ADMIN: OTA-ONA PAROL SÖROVLARI ==============

@login_required
def admin_parent_password_requests(request):
    if not request.user.is_admin:
        return redirect('home')
    
    from .models import ParentPasswordResetRequest
    
    requests_list = ParentPasswordResetRequest.objects.select_related('parent').all()
    
    status_filter = request.GET.get('status')
    if status_filter:
        requests_list = requests_list.filter(status=status_filter)
    
    context = {
        'requests': requests_list,
        'pending_count': ParentPasswordResetRequest.objects.filter(status='pending').count(),
    }
    return render(request, 'accounts/admin/parent_password_requests.html', context)


@login_required
def approve_parent_password_reset(request, pk):
    if not request.user.is_admin:
        return redirect('home')

    from .models import ParentPasswordResetRequest

    reset_request = get_object_or_404(ParentPasswordResetRequest, pk=pk)

    if reset_request.status == 'pending':
        # `new_password` sörov paytida haşlangan — qayta haşlamaymiz.
        reset_request.parent.password = reset_request.new_password
        reset_request.parent.save(update_fields=['password'])

        reset_request.status = 'approved'
        reset_request.reviewed_by = request.user
        reset_request.reviewed_at = timezone.now()
        reset_request.save()

        messages.success(request, f"{reset_request.parent.full_name} paroli yangilandi!")

    return redirect('admin_parent_password_requests')


@login_required
def reject_parent_password_reset(request, pk):
    if not request.user.is_admin:
        return redirect('home')
    
    from .models import ParentPasswordResetRequest
    
    reset_request = get_object_or_404(ParentPasswordResetRequest, pk=pk)
    
    if reset_request.status == 'pending':
        reset_request.status = 'rejected'
        reset_request.reviewed_by = request.user
        reset_request.reviewed_at = timezone.now()
        reset_request.save()
        
        messages.info(request, f"{reset_request.parent.full_name} parol sörovi rad etildi.")
    
    return redirect('admin_parent_password_requests')


@login_required
def admin_premium_management(request):
    if not request.user.is_admin:
        messages.error(request, "Sizda bu sahifaga kiriş huquqi yöq!")
        return redirect('home')

    tab = request.GET.get('tab', 'requests')
    search = request.GET.get('search', '').strip()
    filter_type = request.GET.get('filter', 'all')
    status_filter = request.GET.get('status', 'pending')

    pending_requests = PremiumRequest.objects.filter(status='pending').select_related('user', 'gifted_by')
    all_requests = PremiumRequest.objects.all().select_related('user', 'reviewed_by', 'gifted_by')

    if status_filter and status_filter != 'all':
        all_requests = all_requests.filter(status=status_filter)

    users_qs = CustomUser.objects.filter(role='student').order_by('-premium_purchased_at', 'first_name')

    if filter_type == 'premium':
        users_qs = users_qs.filter(is_premium=True)
    elif filter_type == 'not_premium':
        users_qs = users_qs.filter(is_premium=False)
    elif filter_type == 'expiring':
        from datetime import timedelta as td
        users_qs = users_qs.filter(
            is_premium=True,
            premium_expires_at__lte=timezone.now() + td(days=3),
            premium_expires_at__gt=timezone.now()
        )

    if search:
        users_qs = users_qs.filter(
            Q(first_name__icontains=search) |
            Q(last_name__icontains=search) |
            Q(username__icontains=search) |
            Q(phone__icontains=search)
        )

    if request.method == 'POST':
        action = request.POST.get('action')
        user_id = request.POST.get('user_id')

        try:
            with transaction.atomic():
                if action == 'give_premium':
                    user = CustomUser.objects.select_for_update().get(pk=user_id, role='student')
                    plan = request.POST.get('plan', 'monthly')
                    from accounts.models import PremiumRequest as PR
                    days = PR.PLAN_DAYS.get(plan, 30)

                    now = timezone.now()
                    if user.is_premium and user.premium_expires_at and user.premium_expires_at > now:
                        user.premium_expires_at = user.premium_expires_at + timedelta(days=days)
                    else:
                        user.is_premium = True
                        user.premium_purchased_at = now
                        user.premium_expires_at = now + timedelta(days=days)

                    user.save(update_fields=['is_premium', 'premium_purchased_at', 'premium_expires_at'])

                    plan_name = dict(PR.PLAN_CHOICES).get(plan, plan)
                    Message.objects.create(
                        sender=request.user,
                        recipient=user,
                        title="Premium berildi ✅",
                        content=f"Sizga admin tomonidan {plan_name} Premium obuna berildi!\nMuddati: {user.premium_expires_at.strftime('%d.%m.%Y %H:%M')} gaça.",
                        message_type='system'
                    )
                    messages.success(request, f"{user.full_name}ga {plan_name} Premium berildi!")

                elif action == 'remove_premium':
                    user = CustomUser.objects.select_for_update().get(pk=user_id, role='student')
                    if user.is_premium:
                        user.is_premium = False
                        user.premium_purchased_at = None
                        user.premium_expires_at = None
                        user.save(update_fields=['is_premium', 'premium_purchased_at', 'premium_expires_at'])

                        Message.objects.create(
                            sender=request.user,
                            recipient=user,
                            title="Premium olib taşlandi",
                            content="Sizning Premium obunangiz admin tomonidan olib taşlandi.",
                            message_type='system'
                        )
                        messages.success(request, f"{user.full_name}dan Premium olib taşlandi!")

        except CustomUser.DoesNotExist:
            messages.error(request, "Foydalanuvçi topilmadi!")

        return redirect(f"{request.path}?tab={tab}&filter={filter_type}&search={search}")

    premium_count = CustomUser.objects.filter(role='student', is_premium=True).count()
    total_students = CustomUser.objects.filter(role='student').count()
    pending_count = PremiumRequest.objects.filter(status='pending').count()
    expiring_soon = CustomUser.objects.filter(
        role='student', is_premium=True,
        premium_expires_at__lte=timezone.now() + timedelta(days=3),
        premium_expires_at__gt=timezone.now()
    ).count()

    paginator = Paginator(users_qs, 25)
    page_number = request.GET.get('page')
    users = paginator.get_page(page_number)

    context = {
        'users': users,
        'premium_count': premium_count,
        'total_students': total_students,
        'pending_count': pending_count,
        'expiring_soon': expiring_soon,
        'pending_requests': pending_requests,
        'all_requests': all_requests[:100],
        'search': search,
        'filter_type': filter_type,
        'tab': tab,
        'status_filter': status_filter,
    }

    return render(request, 'accounts/admin/premium_management.html', context)


@login_required
def approve_premium_request(request, pk):
    if not request.user.is_admin:
        return redirect('home')

    prem_req = get_object_or_404(PremiumRequest, pk=pk)

    if prem_req.status != 'pending':
        messages.info(request, "Bu sörov allaqaçon körib çiqilgan.")
        return redirect('admin_premium_management')

    with transaction.atomic():
        prem_req.status = 'approved'
        prem_req.reviewed_by = request.user
        prem_req.reviewed_at = timezone.now()
        prem_req.save()

        user = CustomUser.objects.select_for_update().get(pk=prem_req.user_id)
        days = prem_req.duration_days
        now = timezone.now()

        if user.is_premium and user.premium_expires_at and user.premium_expires_at > now:
            user.premium_expires_at = user.premium_expires_at + timedelta(days=days)
        else:
            user.is_premium = True
            user.premium_purchased_at = now
            user.premium_expires_at = now + timedelta(days=days)

        user.save(update_fields=['is_premium', 'premium_purchased_at', 'premium_expires_at'])

        until = user.premium_expires_at.strftime('%d.%m.%Y %H:%M')
        gifter = prem_req.gifted_by
        if gifter:
            Message.objects.create(
                sender=gifter,
                recipient=user,
                title="🎁 Sizga Premium sovğa qilindi!",
                content=f"{gifter.full_name} sizga {prem_req.get_plan_display()} Premium sovğa qildi!\nMuddati: {until} gaça amal qiladi.",
                message_type='achievement'
            )
            Message.objects.create(
                sender=request.user,
                recipient=gifter,
                title="Premium sovğangiz tasdiqlandi ✅",
                content=f"{user.full_name}ga yuborgan {prem_req.get_plan_display()} Premium sovğangiz faollaşdi.",
                message_type='system'
            )
        else:
            Message.objects.create(
                sender=request.user,
                recipient=user,
                title="Premium tasdiqlandi ✅",
                content=f"Sizning {prem_req.get_plan_display()} Premium sörovingiz tasdiqlandi!\nMuddati: {until} gaça amal qiladi.",
                message_type='system'
            )

    messages.success(request, f"{prem_req.user.full_name}ning Premium sörovi tasdiqlandi!")
    return redirect('admin_premium_management')


@login_required
def reject_premium_request(request, pk):
    if not request.user.is_admin:
        return redirect('home')

    prem_req = get_object_or_404(PremiumRequest, pk=pk)

    if prem_req.status != 'pending':
        messages.info(request, "Bu sörov allaqaçon körib çiqilgan.")
        return redirect('admin_premium_management')

    with transaction.atomic():
        # Holatni şartli UPDATE bilan band qilamiz: tugma ikki marta bosilsa
        # yoki sörov takrorlansa, coin ikki marta qaytarilmasin.
        claimed = PremiumRequest.objects.filter(pk=prem_req.pk, status='pending').update(
            status='rejected', reviewed_by=request.user, reviewed_at=timezone.now()
        )
        if not claimed:
            messages.info(request, "Bu sörov allaqaçon körib çiqilgan.")
            return redirect('admin_premium_management')
        prem_req.refresh_from_db()

        # Sovğa bölsa coin sovğa qiluvçidan yeçilgan — unga qaytaramiz.
        payer_id = prem_req.gifted_by_id or prem_req.user_id
        user = CustomUser.objects.select_for_update().get(pk=payer_id)
        CustomUser.objects.filter(pk=user.pk).update(coins=F('coins') + prem_req.price)
        user.refresh_from_db(fields=['coins'])

        what = (f"{prem_req.user.full_name}ga yuborgan {prem_req.get_plan_display()} Premium sovğangiz"
                if prem_req.gifted_by_id else f"Sizning {prem_req.get_plan_display()} Premium sörovingiz")
        from coin.models import CoinTransaction
        CoinTransaction.objects.create(
            user=user,
            amount=prem_req.price,
            action='add',
            reason=f"Premium {'sovğa' if prem_req.gifted_by_id else 'sörov'} rad etildi - {prem_req.price} coin qaytarildi"
        )

        Message.objects.create(
            sender=request.user,
            recipient=user,
            title="Premium sörov rad etildi",
            content=f"{what} rad etildi.\n{prem_req.price} coin hisobingizga qaytarildi.",
            message_type='system'
        )

    messages.success(request, f"{prem_req.user.full_name}ning Premium sörovi rad etildi. Coinlar qaytarildi.")
    return redirect('admin_premium_management')


# ============== SECTION MANAGEMENT ==============

@login_required
def admin_sections_management(request):
    """Bölimlar boşqaruvi - saytdagi barça bölimlarni bloklab/yoqlab qöyiş"""
    if not request.user.is_admin:
        return redirect('home')
    
    # Database dan bloklangan bölimlarni öqiş
    blocked_db = BlockedSection.objects.all()
    
    # Har bölim uçun default dict yaratiş
    sections_dict = {}
    
    # Barça bölimlar uçun default status qöyiş
    for section_code, section_name in BlockedSection.SECTION_CHOICES:
        sections_dict[section_code] = {
            'is_blocked': False,
            'reason': '',
            'blocked_by': None,
            'blocked_at': None,
        }
    
    # Database dan bloklangan bölimlarni update qiliş
    for bs in blocked_db:
        sections_dict[bs.section] = {
            'is_blocked': bs.is_blocked,
            'reason': bs.reason,
            'blocked_by': bs.blocked_by,
            'blocked_at': bs.blocked_at,
        }
    
    # Template uçun underscore kalit bilan ham saqlaş (hyphenli kalitlar template da işlamaydi)
    sections_dict_safe = {}
    for key, value in sections_dict.items():
        safe_key = key.replace('-', '_')
        sections_dict_safe[safe_key] = value
    
    # Statistika hisoblaş
    total_sections = len(BlockedSection.SECTION_CHOICES)
    blocked_count = BlockedSection.objects.filter(is_blocked=True).count()
    active_count = total_sections - blocked_count
    
    context = {
        'blocked_sections': sections_dict_safe,
        'total_sections': total_sections,
        'blocked_count': blocked_count,
        'active_count': active_count,
    }
    
    return render(request, 'accounts/admin/sections_management.html', context)


@login_required
def toggle_section_block(request, section):
    """Bölimni bloklab yoki yoqlab qöyiş - DATABASE ga saqlaş"""
    import sys
    print(f"DEBUG: toggle_section_block called - User: {request.user}, Admin: {request.user.is_admin}", file=sys.stderr)
    
    if not request.user.is_admin:
        print(f"DEBUG: User not admin", file=sys.stderr)
        return JsonResponse({'error': 'Ruxsatmangan'}, status=403)
    
    if request.method != 'POST':
        print(f"DEBUG: Not POST - Method: {request.method}", file=sys.stderr)
        return JsonResponse({'error': 'POST sörovi kerak'}, status=400)
    
    try:
        # Database dan bloklangan bölimni topib yoki yaratib
        blocked_section, created = BlockedSection.objects.get_or_create(section=section)
        
        reason = request.POST.get('reason', 'Tuzatiş işlari olib borilmoqda')
        
        # Toggle status
        if blocked_section.is_blocked:
            # Yoqib qöyiş
            blocked_section.is_blocked = False
            blocked_section.reason = ''
            blocked = False
            message = f"{section} yoqildi"
        else:
            # Bloklab qöyiş
            blocked_section.is_blocked = True
            blocked_section.reason = reason
            blocked_section.blocked_by = request.user
            blocked = True
            message = f"{section} bloklandi: {reason}"
        
        # Database ga saqlaş
        blocked_section.save()
        
        # Aktivligi log - faqat action_type va description field lar
        # ActivityLog model 'action' emas 'action_type' field boradi
        # va 'details' field yöq, faqat description böladi
        try:
            ActivityLog.objects.create(
                user=request.user,
                action_type='other',
                description=message
            )
        except Exception as e:
            # Log error ignore qil
            pass
        
        return JsonResponse({
            'success': True,
            'blocked': blocked,
            'message': message
        })
    
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': safe_error(e, 'admin_balance_view'),
        }, status=400)


@login_required
def clear_premium_ad(request):
    request.session.pop('show_premium_ad', None)
    return JsonResponse({'ok': True})


@login_required
def clear_ai_ad(request):
    request.session.pop('show_ai_ad', None)
    return JsonResponse({'ok': True})


# ============ ADMIN: ÖQITUVÇILAR BOŞQARUVI ============

@login_required
def admin_teachers(request):
    if not request.user.is_admin:
        return redirect('home')
    from teacher.models import TeacherCertificate, TeacherSalary


@login_required
def admin_teacher_attendance(request):
    if not request.user.is_admin:
        return redirect('home')
    from teacher.models import TeacherAttendance
    from datetime import datetime, date
    
    # Filter parameters
    search_query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '')
    date_filter = request.GET.get('date', '')
    month_filter = request.GET.get('month', '')
    year_filter = request.GET.get('year', '')
    
    # Base queryset
    attendances = TeacherAttendance.objects.all().select_related('teacher', 'created_by')
    
    # Apply filters
    if search_query:
        attendances = attendances.filter(
            Q(teacher__first_name__icontains=search_query) |
            Q(teacher__last_name__icontains=search_query) |
            Q(teacher__username__icontains=search_query) |
            Q(notes__icontains=search_query)
        )
    
    if status_filter:
        attendances = attendances.filter(status=status_filter)
    
    if date_filter:
        try:
            filter_date = datetime.strptime(date_filter, '%Y-%m-%d').date()
            attendances = attendances.filter(date=filter_date)
        except ValueError:
            pass
    
    if month_filter and year_filter:
        try:
            month = int(month_filter)
            year = int(year_filter)
            attendances = attendances.filter(date__month=month, date__year=year)
        except ValueError:
            pass
    
    # Order by date descending
    attendances = attendances.order_by('-date', '-created_at')
    
    # Statistics
    total_attendances = attendances.count()
    present_count = attendances.filter(status='present').count()
    absent_count = attendances.filter(status='absent').count()
    late_count = attendances.filter(status='late').count()
    sick_count = attendances.filter(status='sick').count()
    
    # Get current date for form
    today = date.today()
    
    # Get all teachers for the form
    teachers = CustomUser.objects.filter(role='teacher').order_by('first_name', 'last_name')
    
    context = {
        'attendances': attendances,
        'teachers': teachers,
        'total_attendances': total_attendances,
        'present_count': present_count,
        'absent_count': absent_count,
        'late_count': late_count,
        'sick_count': sick_count,
        'search_query': search_query,
        'status_filter': status_filter,
        'date_filter': date_filter,
        'month_filter': month_filter,
        'year_filter': year_filter,
        'today': today,
    }
    return render(request, 'accounts/admin/teacher_attendance.html', context)


@login_required
def admin_teacher_earnings(request):
    if not request.user.is_admin:
        return redirect('home')
    from teacher.models import TeacherSalary
    from datetime import date, timedelta
    import calendar
    
    # Filter parameters
    search_query = request.GET.get('q', '').strip()
    month_filter = request.GET.get('month', '')
    year_filter = request.GET.get('year', '')
    teacher_filter = request.GET.get('teacher', '')
    
    # Base queryset for teacher salaries
    salaries = TeacherSalary.objects.all().select_related('teacher', 'created_by')
    
    # Apply filters
    if search_query:
        salaries = salaries.filter(
            Q(teacher__first_name__icontains=search_query) |
            Q(teacher__last_name__icontains=search_query) |
            Q(teacher__username__icontains=search_query) |
            Q(description__icontains=search_query)
        )
    
    if teacher_filter:
        salaries = salaries.filter(teacher_id=teacher_filter)
    
    if month_filter and year_filter:
        try:
            month = int(month_filter)
            year = int(year_filter)
            salaries = salaries.filter(month=month, year=year)
        except ValueError:
            pass
    
    # Order by date descending
    salaries = salaries.order_by('-year', '-month', '-created_at')
    
    # Calculate teacher earnings from content
    teachers = CustomUser.objects.filter(role='teacher').order_by('first_name', 'last_name')
    teacher_earnings = []
    
    # Video/test/vazifa narxlari har kasb uçun alohida (Profession'da) —
    # daromad accounts.earnings orqali kasb stavkalari böyiça hisoblanadi.
    from accounts.earnings import period_summary

    for teacher in teachers:
        professions = teacher.teaching_professions.all()
        
        # Get filtered date range or all time if no filter
        if month_filter and year_filter:
            try:
                month = int(month_filter)
                year = int(year_filter)
                month_start = date(year, month, 1)
                if month == 12:
                    month_end = date(year + 1, 1, 1)
                else:
                    month_end = date(year, month + 1, 1)
            except ValueError:
                month_start = date(2020, 1, 1)  # Show all data if invalid filter
                month_end = date.today() + timedelta(days=1)
        else:
            # Show all data when no filter is applied
            month_start = date(2020, 1, 1)
            month_end = date.today() + timedelta(days=1)
        
        # Apply teacher filter if specified
        if teacher_filter and str(teacher.id) != teacher_filter:
            continue  # Skip this teacher if not the selected one
        
        # Kontentdan daromad — har kasbning öz stavkasi böyiça
        summary = period_summary(professions, start=month_start, end=month_end)
        video_count = summary['video']['count']
        video_total = summary['video']['total']
        test_count = summary['test']['count']
        test_total = summary['test']['total']
        hw_count = summary['homework']['count']
        hw_total = summary['homework']['total']
        calculated_earnings = video_total + test_total + hw_total
        
        # Get paid amount for this teacher
        paid_amount = salaries.filter(
            teacher=teacher,
            is_paid=True
        ).aggregate(total=Sum('amount'))['total'] or 0
        
        # Calculate current month earnings
        now = date.today()
        current_month_start = date(now.year, now.month, 1)
        if now.month == 12:
            current_month_end = date(now.year + 1, 1, 1)
        else:
            current_month_end = date(now.year, now.month + 1, 1)
        
        # Calculate previous month earnings
        if now.month == 1:
            prev_month_start = date(now.year - 1, 12, 1)
            prev_month_end = date(now.year, 1, 1)
        else:
            prev_month_start = date(now.year, now.month - 1, 1)
            prev_month_end = date(now.year, now.month, 1)
        
        # Joriy oy daromadi — kasb stavkalari böyiça
        current_summary = period_summary(
            professions, start=current_month_start, end=current_month_end)
        current_video = current_summary['video']['count']
        current_test = current_summary['test']['count']
        current_hw = current_summary['homework']['count']
        current_month_earnings = (current_summary['video']['total'] +
                                  current_summary['test']['total'] +
                                  current_summary['homework']['total'])

        # Ötgan oy daromadi — kasb stavkalari böyiça
        prev_summary = period_summary(
            professions, start=prev_month_start, end=prev_month_end)
        prev_video = prev_summary['video']['count']
        prev_test = prev_summary['test']['count']
        prev_hw = prev_summary['homework']['count']
        prev_month_earnings = (prev_summary['video']['total'] +
                               prev_summary['test']['total'] +
                               prev_summary['homework']['total'])
        
        # Calculate month-over-month change
        earnings_change = current_month_earnings - prev_month_earnings
        earnings_change_percent = ((earnings_change / prev_month_earnings) * 100) if prev_month_earnings > 0 else 0
        
        # Last lesson created by this teacher
        last_lesson = Lesson.objects.filter(
            profession__in=professions,
        ).order_by('-created_at').first()
        
        # Current month detail counts
        current_video_count = current_video
        current_test_count = current_test
        current_hw_count = current_hw
        prev_video_count = prev_video
        prev_test_count = prev_test
        prev_hw_count = prev_hw
        
        teacher_earnings.append({
            'teacher': teacher,
            'video_count': video_count,
            'test_count': test_count,
            'hw_count': hw_count,
            'video_total': video_total,
            'test_total': test_total,
            'hw_total': hw_total,
            'calculated_earnings': calculated_earnings,
            'paid_amount': paid_amount,
            'balance': calculated_earnings - paid_amount,
            'current_month_earnings': current_month_earnings,
            'prev_month_earnings': prev_month_earnings,
            'earnings_change': earnings_change,
            'earnings_change_percent': earnings_change_percent,
            'last_lesson': last_lesson,
            'current_video_count': current_video_count,
            'current_test_count': current_test_count,
            'current_hw_count': current_hw_count,
            'prev_video_count': prev_video_count,
            'prev_test_count': prev_test_count,
            'prev_hw_count': prev_hw_count,
        })
    
    # Statistics
    total_calculated = sum(t['calculated_earnings'] for t in teacher_earnings)
    total_paid = sum(t['paid_amount'] for t in teacher_earnings)
    total_balance = total_calculated - total_paid
    
    # Get current date for filters
    now = date.today()
    
    month_names = {1: 'Yanvar', 2: 'Fevral', 3: 'Mart', 4: 'Aprel',
                   5: 'May', 6: 'Iyun', 7: 'Iyul', 8: 'Avgust',
                   9: 'Sentyabr', 10: 'Oktyabr', 11: 'Noyabr', 12: 'Dekabr'}
    
    current_month_name = month_names.get(now.month, '')
    if now.month == 1:
        prev_month_name = month_names.get(12, '')
    else:
        prev_month_name = month_names.get(now.month - 1, '')
    
    context = {
        'teacher_earnings': teacher_earnings,
        'salaries': salaries,
        'total_calculated': total_calculated,
        'total_paid': total_paid,
        'total_balance': total_balance,
        'search_query': search_query,
        'month_filter': month_filter,
        'year_filter': year_filter,
        'teacher_filter': teacher_filter,
        'teachers': teachers,
        'current_month': now.month,
        'current_year': now.year,
        'current_month_name': current_month_name,
        'prev_month_name': prev_month_name,
    }
    return render(request, 'accounts/admin/teacher_earnings.html', context)


@login_required
def admin_teachers(request):
    if not request.user.is_admin:
        return redirect('home')
    from teacher.models import TeacherCertificate, TeacherSalary
    from datetime import datetime

    search_query = request.GET.get('q', '').strip()
    teachers = CustomUser.objects.filter(role='teacher').order_by('-date_joined')
    if search_query:
        teachers = teachers.filter(
            Q(first_name__icontains=search_query) | Q(last_name__icontains=search_query) |
            Q(username__icontains=search_query) | Q(phone__icontains=search_query)
        )

    now = timezone.now()
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)
    current_month = now.month
    current_year = now.year

    teachers_data = []
    for t in teachers:
        professions = t.teaching_professions.all()
        courses_count = professions.count()
        students_count = sum(p.enrollments.filter(user__role='student').count() for p in professions)
        certs_count = TeacherCertificate.objects.filter(teacher=t).count()

        # Lessons created by this teacher
        lessons_created = Lesson.objects.filter(created_by=t).count()
        lessons_this_month = Lesson.objects.filter(created_by=t, created_at__gte=month_ago).count()
        lessons_this_week = Lesson.objects.filter(created_by=t, created_at__gte=week_ago).count()

        # Homework graded
        hw_graded_total = HomeworkSubmission.objects.filter(graded_by=t).count()
        hw_graded_month = HomeworkSubmission.objects.filter(graded_by=t, graded_at__gte=month_ago).count()
        hw_graded_week = HomeworkSubmission.objects.filter(graded_by=t, graded_at__gte=week_ago).count()
        hw_pending = HomeworkSubmission.objects.filter(
            homework__lesson__profession__in=professions, status='pending'
        ).count()

        # Speaking graded
        sp_graded_total = SpeakingSubmission.objects.filter(graded_by=t).count()
        sp_graded_month = SpeakingSubmission.objects.filter(graded_by=t, graded_at__gte=month_ago).count()

        # Online activity
        is_online = t.is_online
        last_activity = t.last_activity
        total_online_minutes = t.total_online_time or 0

        # Activity log
        activities_week = ActivityLog.objects.filter(user=t, created_at__gte=week_ago).count()
        activities_month = ActivityLog.objects.filter(user=t, created_at__gte=month_ago).count()
        login_count_week = ActivityLog.objects.filter(user=t, action_type='login', created_at__gte=week_ago).count()
        login_count_month = ActivityLog.objects.filter(user=t, action_type='login', created_at__gte=month_ago).count()

        # Dictionary words added
        dict_words_total = DictionaryWord.objects.filter(
            dictionary__lesson__profession__in=professions
        ).count()

        # Salary / income
        salary_total = TeacherSalary.objects.filter(teacher=t, is_paid=True).aggregate(
            total=Sum('amount')
        )['total'] or 0
        salary_month = TeacherSalary.objects.filter(
            teacher=t, is_paid=True, month=current_month, year=current_year
        ).aggregate(total=Sum('amount'))['total'] or 0
        salary_unpaid = TeacherSalary.objects.filter(teacher=t, is_paid=False).aggregate(
            total=Sum('amount')
        )['total'] or 0

        # Videos uploaded
        videos_count = VideoLesson.objects.filter(lesson__created_by=t).count()

        # Tests created
        tests_count = Test.objects.filter(lesson__created_by=t).count()

        teachers_data.append({
            'user': t,
            'courses_count': courses_count,
            'students_count': students_count,
            'certs_count': certs_count,
            'lessons_created': lessons_created,
            'lessons_this_month': lessons_this_month,
            'lessons_this_week': lessons_this_week,
            'hw_graded_total': hw_graded_total,
            'hw_graded_month': hw_graded_month,
            'hw_graded_week': hw_graded_week,
            'hw_pending': hw_pending,
            'sp_graded_total': sp_graded_total,
            'sp_graded_month': sp_graded_month,
            'is_online': is_online,
            'last_activity': last_activity,
            'total_online_minutes': total_online_minutes,
            'activities_week': activities_week,
            'activities_month': activities_month,
            'login_count_week': login_count_week,
            'login_count_month': login_count_month,
            'dict_words_total': dict_words_total,
            'salary_total': salary_total,
            'salary_month': salary_month,
            'salary_unpaid': salary_unpaid,
            'videos_count': videos_count,
            'tests_count': tests_count,
        })
    return render(request, 'accounts/admin/teachers.html', {
        'teachers_data': teachers_data, 'search_query': search_query, 'total_count': len(teachers_data),
    })


@login_required
def export_teachers_pdf(request):
    if not request.user.is_admin:
        return redirect('home')
    from teacher.models import TeacherSalary
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from io import BytesIO

    now = timezone.now()
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    teachers = CustomUser.objects.filter(role='teacher').order_by('first_name')

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), topMargin=25, bottomMargin=25,
                            leftMargin=20, rightMargin=20)
    elements = []
    styles = getSampleStyleSheet()

    heading_style = ParagraphStyle(
        'CustomHeading', parent=styles['Heading2'],
        spaceAfter=10, spaceBefore=15, textColor=colors.HexColor('#10b981')
    )
    small_style = ParagraphStyle(
        'Small', parent=styles['Normal'], fontSize=7, leading=9,
    )

    elements.append(Paragraph("Öqituvçilar statistikasi - Töliq hisobot", styles['Title']))
    elements.append(Paragraph(f"Sana: {now.strftime('%d.%m.%Y %H:%M')}", styles['Normal']))
    elements.append(Spacer(1, 15))

    # Summary table
    header = [
        Paragraph('Öqituvçi', small_style),
        Paragraph('Kurslar', small_style),
        Paragraph('Öquv.', small_style),
        Paragraph('Darslar', small_style),
        Paragraph('Hafta/Oy', small_style),
        Paragraph('Vazifa tekş.', small_style),
        Paragraph('Hafta/Oy', small_style),
        Paragraph('Kutilmoqda', small_style),
        Paragraph('Speaking', small_style),
        Paragraph('Videolar', small_style),
        Paragraph('Testlar', small_style),
        Paragraph('Luğat', small_style),
        Paragraph('Login H/O', small_style),
        Paragraph('Online (daq)', small_style),
        Paragraph('Daromad (söm)', small_style),
    ]
    data = [header]

    for t in teachers:
        professions = t.teaching_professions.all()
        courses_count = professions.count()
        students_count = sum(p.enrollments.filter(user__role='student').count() for p in professions)
        lessons_total = Lesson.objects.filter(created_by=t).count()
        lessons_week = Lesson.objects.filter(created_by=t, created_at__gte=week_ago).count()
        lessons_month = Lesson.objects.filter(created_by=t, created_at__gte=month_ago).count()
        hw_total = HomeworkSubmission.objects.filter(graded_by=t).count()
        hw_week = HomeworkSubmission.objects.filter(graded_by=t, graded_at__gte=week_ago).count()
        hw_month = HomeworkSubmission.objects.filter(graded_by=t, graded_at__gte=month_ago).count()
        hw_pending = HomeworkSubmission.objects.filter(
            homework__lesson__profession__in=professions, status='pending'
        ).count()
        sp_total = SpeakingSubmission.objects.filter(graded_by=t).count()
        videos = VideoLesson.objects.filter(lesson__created_by=t).count()
        tests = Test.objects.filter(lesson__created_by=t).count()
        dict_words = DictionaryWord.objects.filter(dictionary__lesson__profession__in=professions).count()
        login_w = ActivityLog.objects.filter(user=t, action_type='login', created_at__gte=week_ago).count()
        login_m = ActivityLog.objects.filter(user=t, action_type='login', created_at__gte=month_ago).count()
        online_min = t.total_online_time or 0
        salary_total = TeacherSalary.objects.filter(teacher=t, is_paid=True).aggregate(
            total=Sum('amount'))['total'] or 0

        row = [
            Paragraph(t.full_name, small_style),
            Paragraph(str(courses_count), small_style),
            Paragraph(str(students_count), small_style),
            Paragraph(str(lessons_total), small_style),
            Paragraph(f"{lessons_week}/{lessons_month}", small_style),
            Paragraph(str(hw_total), small_style),
            Paragraph(f"{hw_week}/{hw_month}", small_style),
            Paragraph(str(hw_pending), small_style),
            Paragraph(str(sp_total), small_style),
            Paragraph(str(videos), small_style),
            Paragraph(str(tests), small_style),
            Paragraph(str(dict_words), small_style),
            Paragraph(f"{login_w}/{login_m}", small_style),
            Paragraph(str(online_min), small_style),
            Paragraph(f"{salary_total:,.0f}", small_style),
        ]
        data.append(row)

    col_widths = [85, 40, 40, 40, 45, 50, 45, 50, 45, 45, 40, 40, 50, 55, 70]
    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#10b981')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#dee2e6')),
        ('PADDING', (0, 0), (-1, -1), 4),
        ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f0fdf4')]),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 7),
    ]))
    elements.append(table)

    doc.build(elements)
    buffer.seek(0)
    response = HttpResponse(buffer, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="teachers_stats_{now.strftime("%Y%m%d")}.pdf"'
    return response


@login_required
def admin_teacher_ai_toggle(request, pk):
    """Öqituvçining AI funksiyasidan foydalaniş ruxsatini yoqish/öchirish (AJAX yoki POST)."""
    if not request.user.is_admin:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'error': 'Ruxsat yöq'}, status=403)
        return redirect('home')

    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': "POST sörov kerak"}, status=405)

    teacher = get_object_or_404(CustomUser, pk=pk, role='teacher')
    teacher.ai_access_enabled = not teacher.ai_access_enabled
    teacher.save(update_fields=['ai_access_enabled'])

    new_state = teacher.ai_access_enabled
    msg = (
        f"{teacher.full_name} uçun AI funksiyasi "
        f"{'yoqildi' if new_state else 'bloklandi'}."
    )

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'success': True,
            'new_state': new_state,
            'message': msg,
        })

    messages.success(request, msg)
    return redirect('admin_teachers')


@login_required
def admin_teacher_profile(request, pk):
    if not request.user.is_admin:
        return redirect('home')
    from teacher.models import TeacherCertificate

    teacher = get_object_or_404(CustomUser, pk=pk, role='teacher')
    courses = teacher.teaching_professions.all()
    certificates = TeacherCertificate.objects.filter(teacher=teacher, is_visible=True)

    total_students = 0
    courses_data = []
    for p in courses:
        sc = p.enrollments.filter(user__role='student').count()
        total_students += sc
        courses_data.append({'profession': p, 'student_count': sc})

    context = {
        'teacher': teacher,
        'courses': courses,
        'courses_data': courses_data,
        'certificates': certificates,
        'total_students': total_students,
        'certificates_count': certificates.count(),
        'courses_count': courses.count(),
    }
    return render(request, 'accounts/admin/teacher_profile.html', context)


@login_required
def admin_teacher_certificates(request):
    if not request.user.is_admin:
        return redirect('home')
    from teacher.models import TeacherCertificate
    teacher_id = request.GET.get('teacher', '')
    certs = TeacherCertificate.objects.all().select_related('teacher')
    if teacher_id:
        try: certs = certs.filter(teacher_id=int(teacher_id))
        except (ValueError, TypeError): pass
    teachers = CustomUser.objects.filter(role='teacher').order_by('first_name')
    return render(request, 'accounts/admin/teacher_certificates.html', {
        'certificates': certs, 'teachers': teachers, 'teacher_id': teacher_id,
    })


@login_required
def admin_teacher_certificate_add(request):
    if not request.user.is_admin:
        return redirect('home')
    from teacher.models import TeacherCertificate
    teachers = CustomUser.objects.filter(role='teacher').order_by('first_name')
    if request.method == 'POST':
        teacher_id = request.POST.get('teacher')
        title = request.POST.get('title', '').strip()
        description = request.POST.get('description', '')
        issued_date = request.POST.get('issued_date') or None
        file = request.FILES.get('file')
        if not title or not file or not teacher_id:
            messages.error(request, "Öqituvçi, nom va faylni kiriting.")
            return render(request, 'accounts/admin/teacher_certificate_form.html', {'teachers': teachers})
        teacher = get_object_or_404(CustomUser, pk=teacher_id, role='teacher')
        TeacherCertificate.objects.create(
            teacher=teacher, title=title, description=description, issued_date=issued_date, file=file,
        )
        messages.success(request, f"{teacher.full_name}ga sertifikat qöşildi!")
        return redirect('admin_teacher_certificates')
    return render(request, 'accounts/admin/teacher_certificate_form.html', {'teachers': teachers})


@login_required
def admin_teacher_certificate_delete(request, pk):
    if not request.user.is_admin:
        return redirect('home')
    from teacher.models import TeacherCertificate
    cert = get_object_or_404(TeacherCertificate, pk=pk)
    if request.method == 'POST':
        cert.delete()
        messages.success(request, "Sertifikat öçirildi!")
    return redirect('admin_teacher_certificates')


def ariza_view(request):
    """Kursga yoziliş arizasi — oçiq sahifa"""
    professions = Profession.objects.filter(is_blocked=False)
    success = False
    
    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        raw_phone = request.POST.get('phone', '').strip()
        phone_digits = ''.join(c for c in raw_phone if c.isdigit())
        if phone_digits.startswith('998') and len(phone_digits) == 12:
            phone_digits = phone_digits[3:]
        phone = '+998' + phone_digits if phone_digits else raw_phone
        course_interest = request.POST.get('course_interest', '').strip()
        
        if first_name and last_name and phone:
            application = CourseApplication.objects.create(
                first_name=first_name,
                last_name=last_name,
                phone=phone,
                course_interest=course_interest
            )
            
            notify_application_recipients(
                f"📋 <b>Yangi ariza keldi!</b>\n\n"
                f"👤 {first_name} {last_name}\n"
                f"📱 {phone}\n"
                f"📚 Kurs: {course_interest or 'Belgilanmagan'}\n\n"
                f"📞 Tez orada boğlaning!"
            )
            success = True

    return render(request, 'accounts/ariza.html', {
        'professions': professions,
        'success': success,
    })






@login_required
def admin_applications(request):
    """Admin: Arizalar röyxati"""
    if not request.user.is_admin:
        return redirect('home')
    
    if request.GET.get('type') == 'shaharcha':
        from django.http import Http404
        raise Http404
    app_type = 'course'
        
    base_qs = CourseApplication.objects.filter(is_shaharcha=(app_type == 'shaharcha'))
    applications = base_qs.select_related('handled_by').all()
    
    status_filter = request.GET.get('status', '')
    if status_filter:
        applications = applications.filter(status=status_filter)
    
    q = request.GET.get('q', '').strip()
    if q:
        from django.db.models import Q
        if app_type == 'shaharcha':
            applications = applications.filter(
                Q(first_name__icontains=q) |
                Q(last_name__icontains=q) |
                Q(phone__icontains=q) |
                Q(neighborhood__icontains=q) |
                Q(school_number__icontains=q)
            )
        else:
            applications = applications.filter(
                Q(first_name__icontains=q) |
                Q(last_name__icontains=q) |
                Q(phone__icontains=q) |
                Q(course_interest__icontains=q)
            )
    
    from django.core.paginator import Paginator
    paginator = Paginator(applications, 20)
    page = request.GET.get('page')
    applications = paginator.get_page(page)
    
    context = {
        'applications': applications,
        'status_filter': status_filter,
        'search': q,
        'app_type': app_type,
        'total_new': base_qs.filter(status='new').count(),
        'total_all': base_qs.count(),
        'total_contacted': base_qs.filter(status='contacted').count(),
        'total_enrolled': base_qs.filter(status='enrolled').count(),
        'total_rejected': base_qs.filter(status='rejected').count(),
    }
    return render(request, 'accounts/admin/applications.html', context)


@login_required
def admin_application_status(request, pk):
    """Admin: Ariza statusini özgartiriş"""
    if not request.user.is_admin:
        return redirect('home')
    
    application = get_object_or_404(CourseApplication, pk=pk, is_shaharcha=False)
    
    if request.method == 'POST':
        new_status = request.POST.get('status')
        note = request.POST.get('admin_note', '')
        if new_status in dict(CourseApplication.STATUS_CHOICES):
            application.status = new_status
            application.admin_note = note
            application.handled_by = request.user
            application.handled_at = timezone.now()
            application.save()
            messages.success(request, "Ariza holati yangilandi!")
            
    referer = request.META.get('HTTP_REFERER')
    if referer:
        return redirect(referer)
    return redirect('admin_applications')


@login_required
def admin_certificates(request):
    """Admin: Öquvçi sertifikatlarini boşqariş"""
    if not request.user.is_admin:
        return redirect('home')
    
    certificates = Certificate.objects.select_related('student', 'profession', 'issued_by').all()
    
    # Filter by status
    status_filter = request.GET.get('status', '')
    if status_filter:
        certificates = certificates.filter(status=status_filter)
    
    # Filter by profession
    profession_filter = request.GET.get('profession', '')
    if profession_filter:
        certificates = certificates.filter(profession_id=profession_filter)
    
    # Search
    q = request.GET.get('search', '').strip()
    if q:
        certificates = certificates.filter(
            Q(student__first_name__icontains=q) |
            Q(student__last_name__icontains=q) |
            Q(student__username__icontains=q) |
            Q(certificate_id__icontains=q)
        )
    
    # Stats
    total_certificates = Certificate.objects.count()
    active_certificates = Certificate.objects.filter(status='active').count()
    expired_certificates = Certificate.objects.filter(status='expired').count()
    revoked_certificates = Certificate.objects.filter(status='revoked').count()
    frozen_certificates = Certificate.objects.filter(status='frozen').count()
    
    professions = Profession.objects.all()
    students = CustomUser.objects.filter(role='student').order_by('first_name', 'last_name')

    # Kurslar böyiça seriya/muddat sozlamalari (avtomatik beriş uçun)
    from .models import CertificateSettings, CertificateDesign
    _settings_map = {
        s.profession_id: s for s in CertificateSettings.objects.all()
    }
    course_settings = []
    for p in professions:
        s = _settings_map.get(p.pk)
        course_settings.append({
            'profession': p,
            'series_code': s.series_code if s else '',
            'validity_years': s.validity_years if s else 2,
            'design_id': s.design_id if s else None,
            'issued_count': p.certificates.count(),
        })

    from django.core.paginator import Paginator
    paginator = Paginator(certificates, 20)
    page = request.GET.get('page')
    certificates = paginator.get_page(page)

    context = {
        'certificates': certificates,
        'status_filter': status_filter,
        'profession_filter': profession_filter,
        'search': q,
        'total_certificates': total_certificates,
        'active_certificates': active_certificates,
        'expired_certificates': expired_certificates,
        'revoked_certificates': revoked_certificates,
        'frozen_certificates': frozen_certificates,
        'professions': professions,
        'students': students,
        'course_settings': course_settings,
        'certificate_designs': CertificateDesign.objects.only('id', 'name'),
    }
    return render(request, 'accounts/admin/certificates.html', context)


@login_required
def admin_certificate_issue(request):
    """Admin: Yangi sertifikat beriş"""
    if not request.user.is_admin:
        return redirect('home')
    
    students = CustomUser.objects.filter(role='student').order_by('first_name', 'last_name')
    professions = Profession.objects.all()
    
    if request.method == 'POST':
        form = CertificateForm(request.POST, request.FILES)
        student_id = request.POST.get('student')
        profession_id = request.POST.get('profession')
        
        if form.is_valid() and student_id and profession_id:
            student = get_object_or_404(CustomUser, pk=student_id, role='student')
            profession = get_object_or_404(Profession, pk=profession_id)
            cert = form.save(commit=False)
            cert.student = student
            cert.profession = profession
            cert.issued_by = request.user
            cert.save()
            
            # Öquvçiga çiroyli tabrik xabari yuboriş
            Message.objects.create(
                recipient=student,
                sender=request.user,
                title="🎓🎉 Tabriklaymiz! Sertifikat berildi!",
                content=(
                    f"🌟🌟🌟 TABRIKLAYMIZ! 🌟🌟🌟\n\n"
                    f"Hurmatli {student.full_name}!\n\n"
                    f"🎓 Sizga \"{profession.name}\" yönalişi böyiça SERTIFIKAT berildi!\n\n"
                    f"📋 Ma'lumotlar:\n"
                    f"   🆔 ID raqam: {student.id}\n"
                    f"   📚 Yönaliş: {profession.name}\n"
                    f"   🏅 Sertifikat ID: {cert.certificate_id}\n"
                    f"   📅 Berilgan sana: {cert.valid_from}\n"
                    f"   ✅ Holat: {cert.get_status_display()}\n\n"
                    f"🎊 Siz bu yönalişni muvaffaqiyatli tugatdingiz! Bu sizning mehnat va bilimingizning natijasi.\n\n"
                    f"🚀 Yangi kurslarda ham kutib qolamiz! IT Creative Academy sizga yanada köproq bilim va tajriba taqdim etişga tayyor.\n\n"
                    f"📜 Sertifikatingizni profil sahifangizdagi \"Sertifikatlarim\" bölimida körişingiz mumkin.\n\n"
                    f"💜 Hurmat bilan,\nIT Creative Academy jamoasi"
                ),
                message_type='achievement'
            )
            
            # Telegram orqali çiroyli tabrik xabari
            if student.telegram_id:
                telegram_text = (
                    f"🎓🎉🎊 <b>TABRIKLAYMIZ!</b> 🎊🎉🎓\n\n"
                    f"Hurmatli <b>{student.full_name}</b>!\n\n"
                    f"✨ Sizga <b>«{profession.name}»</b> yönalişi böyiça <b>SERTIFIKAT</b> berildi!\n\n"
                    f"📋 <b>Ma'lumotlar:</b>\n"
                    f"┌ 🆔 ID raqam: <b>{student.id}</b>\n"
                    f"├ 📚 Yönaliş: <b>{profession.name}</b>\n"
                    f"├ 🏅 Sertifikat: <b>{cert.certificate_id}</b>\n"
                    f"└ 📅 Sana: <b>{cert.valid_from}</b>\n\n"
                    f"🌟 Siz bu yönalişni muvaffaqiyatli tugatdingiz! Bu sizning mehnat va bilimingizning natijasi.\n\n"
                    f"🚀 <b>Yangi kurslarda ham kutib qolamiz!</b>\n"
                    f"IT Creative Academy sizga yanada köproq bilim va tajriba taqdim etişga tayyor.\n\n"
                    f"💜 <i>IT Creative Academy jamoasi</i>"
                )
                notify_user(student.telegram_id, telegram_text)
            
            # Adminlarga xabar
            notify_admins(
                f"🎓 <b>Sertifikat berildi!</b>\n\n"
                f"👤 Öquvçi: <b>{student.full_name}</b>\n"
                f"📚 Yönaliş: <b>{profession.name}</b>\n"
                f"🏅 Sertifikat: <b>{cert.certificate_id}</b>"
            )
            
            # Guruhga tabrik
            notify_group(
                f"🎓🎉 <b>TABRIKLAYMIZ!</b> 🎉🎓\n\n"
                f"🌟 <b>{student.full_name}</b> 🌟\n\n"
                f"<b>«{profession.name}»</b> yönalişini muvaffaqiyatli tugatdi va <b>SERTIFIKAT</b> oldi! 🏅\n\n"
                f"🎊 Tabriklaymiz va yangi kurslarda kutib qolamiz! 🚀\n\n"
                f"💜 <b>IT Creative Academy</b>"
            )
            
            messages.success(request, f"Sertifikat muvaffaqiyatli berildi! ID: {cert.certificate_id}")
            return redirect('admin_certificates')
        else:
            messages.error(request, "Iltimos, barça maydonlarni töldiring!")
    else:
        form = CertificateForm()
    
    return render(request, 'accounts/admin/certificate_form.html', {
        'students': students,
        'professions': professions,
        'form': form,
        'is_edit': False,
    })


@login_required
def admin_certificate_status(request, pk):
    """Admin: Sertifikat holatini özgartiriş"""
    if not request.user.is_admin:
        return redirect('home')
    
    cert = get_object_or_404(Certificate, pk=pk)
    
    if request.method == 'POST':
        new_status = request.POST.get('status')
        if new_status in dict(Certificate.STATUS_CHOICES):
            old_status = cert.get_status_display()
            cert.status = new_status
            cert.save()

            # PDF ni qayta yaratiş — holat sertifikat faylida ham aks etadi
            try:
                from .certificates import regenerate_certificate_file
                regenerate_certificate_file(cert)
            except Exception:
                pass

            # Öquvçiga xabar (platformadagi öquvçi bölsa)
            status_display = cert.get_status_display()
            if cert.student:
                Message.objects.create(
                    recipient=cert.student,
                    sender=request.user,
                    title="📋 Sertifikat holati özgardi",
                    content=f"Sizning \"{cert.course_name}\" kursi böyiça sertifikatingiz holati özgardi.\n\nSertifikat ID: {cert.certificate_id}\nOldingi holat: {old_status}\nYangi holat: {status_display}",
                    message_type='system'
                )
            
            messages.success(request, f"Sertifikat holati \"{status_display}\" ga özgartirildi!")
    
    return redirect('admin_certificates')


@login_required
def admin_certificate_delete(request, pk):
    """Admin: Sertifikatni öçiriş"""
    if not request.user.is_admin:
        return redirect('home')
    
    cert = get_object_or_404(Certificate, pk=pk)
    
    if request.method == 'POST':
        cert.delete()
        messages.success(request, "Sertifikat öçirildi!")
    
    return redirect('admin_certificates')


@login_required
def final_test_payment(request, pk):
    """Yakuniy test uçun tölov çeki yuklaş"""
    test = get_object_or_404(Test, pk=pk, test_type='final')
    from quiz.models import Quiz
    migrated = Quiz.objects.filter(lesson=test.lesson).first()
    if migrated:
        return redirect('quizui:quiz_retry_payment_request', pk=migrated.pk)
    
    # Birinçi marta bölsa — bepul
    existing_result = TestResult.objects.filter(test=test, student=request.user).exists()
    if not existing_result:
        return redirect('start_test', pk=test.pk)
    
    # Allaqaçon kutilayotgan tölov bormi
    pending_payment = FinalTestPayment.objects.filter(
        student=request.user, test=test, status='pending'
    ).first()
    
    if request.method == 'POST' and not pending_payment:
        receipt = request.FILES.get('receipt_image')
        if receipt:
            FinalTestPayment.objects.create(
                student=request.user,
                test=test,
                receipt_image=receipt,
            )
            messages.success(request, "Tölov çeki yuklandi! Admin tasdiqlaşini kuting.")
            return redirect('lesson_view', pk=test.lesson.pk)
        else:
            messages.error(request, "Iltimos, tölov çekini yuklang.")
    
    approved_payment = FinalTestPayment.objects.filter(
        student=request.user, test=test, status='approved'
    ).exists()

    # Oxirgi rad etilgan tölov — öquvçiga sababini körsatiş uçun
    rejected_payment = None
    if not pending_payment and not approved_payment:
        rejected_payment = FinalTestPayment.objects.filter(
            student=request.user, test=test, status='rejected'
        ).first()

    context = {
        'test': test,
        'pending_payment': pending_payment,
        'approved_payment': approved_payment,
        'rejected_payment': rejected_payment,
    }
    from .exam_payments import exam_context
    context.update(exam_context(request.user, 'test', test.pk))
    return render(request, 'accounts/lessons/final_test_payment.html', context)


@login_required
def admin_final_test_payments(request):
    """Admin panel: Yakuniy test tölovlarini boşqariş"""
    if request.user.role not in ('admin', 'teacher'):
        return redirect('home')
    
    status_filter = request.GET.get('status', 'pending')
    payments = FinalTestPayment.objects.select_related('student', 'test__lesson__profession', 'reviewed_by')
    
    if status_filter:
        payments = payments.filter(status=status_filter)
    
    # Öqituvçi faqat öz kurslaridagi testlarni köradi
    if request.user.role == 'teacher':
        professions = request.user.teaching_professions.all()
        payments = payments.filter(test__lesson__profession__in=professions)
    
    context = {
        'payments': payments[:100],
        'status_filter': status_filter,
        'pending_count': FinalTestPayment.objects.filter(status='pending').count() if request.user.role == 'admin' else FinalTestPayment.objects.filter(status='pending', test__lesson__profession__in=request.user.teaching_professions.all()).count(),
    }
    
    if request.user.role == 'teacher':
        return render(request, 'teacher/final_test_payments.html', context)
    return render(request, 'accounts/admin/final_test_payments.html', context)


@login_required
def approve_final_test_payment(request, pk):
    """Yakuniy test tölovini tasdiqlaş"""
    if request.user.role not in ('admin', 'teacher'):
        return redirect('home')
    
    payment = get_object_or_404(FinalTestPayment, pk=pk)
    
    # Öqituvçi faqat öz kursi
    if request.user.role == 'teacher':
        professions = request.user.teaching_professions.all()
        if payment.test.lesson.profession not in professions:
            messages.error(request, "Sizda bu tölovni tasdiqlaş huquqi yöq.")
            return redirect('admin_final_test_payments')
    
    if request.method == 'POST':
        payment.status = 'approved'
        payment.reviewed_by = request.user
        payment.reviewed_at = timezone.now()
        payment.save()
        messages.success(request, f"{payment.student.full_name}ning tölovi tasdiqlandi!")
    
    return redirect('admin_final_test_payments')


@login_required
def reject_final_test_payment(request, pk):
    """Yakuniy test tölovini rad etiş"""
    if request.user.role not in ('admin', 'teacher'):
        return redirect('home')
    
    payment = get_object_or_404(FinalTestPayment, pk=pk)
    
    if request.user.role == 'teacher':
        professions = request.user.teaching_professions.all()
        if payment.test.lesson.profession not in professions:
            messages.error(request, "Sizda bu tölovni rad etiş huquqi yöq.")
            return redirect('admin_final_test_payments')
    
    if request.method == 'POST':
        payment.status = 'rejected'
        payment.reviewed_by = request.user
        payment.reviewed_at = timezone.now()
        payment.reject_reason = request.POST.get('reason', '')
        payment.save()
        messages.success(request, f"{payment.student.full_name}ning tölovi rad etildi.")
    
    return redirect('admin_final_test_payments')


@login_required
def admin_ai_checker(request):
    """AI Tekşiruvçi vazifalar boşqaruvi"""
    if not request.user.is_admin:
        return redirect('home')
    
    professions = Profession.objects.all()
    
    # Filter by profession
    profession_id = request.GET.get('course', '')
    
    # Homework vazifalar
    homeworks = Homework.objects.select_related('lesson__profession').all().order_by('lesson__profession__name', 'lesson__title')
    
    # Speaking vazifalar
    speaking_tasks = SpeakingTask.objects.select_related('lesson__profession').all().order_by('lesson__profession__name', 'lesson__title')
    
    if profession_id:
        try:
            homeworks = homeworks.filter(lesson__profession_id=int(profession_id))
            speaking_tasks = speaking_tasks.filter(lesson__profession_id=int(profession_id))
        except (ValueError, TypeError):
            pass
    
    # Statistika
    total_hw = homeworks.count()
    ai_on_hw = homeworks.filter(ai_check=True).count()
    ai_off_hw = total_hw - ai_on_hw
    
    total_sp = speaking_tasks.count()
    ai_on_sp = speaking_tasks.filter(ai_check=True).count()
    ai_off_sp = total_sp - ai_on_sp
    
    # Kurslar böyiça guruhlab
    from collections import OrderedDict
    hw_by_course = OrderedDict()
    for hw in homeworks:
        course_name = hw.lesson.profession.name
        if course_name not in hw_by_course:
            hw_by_course[course_name] = {
                'tasks': [], 'ai_enabled': hw.lesson.profession.ai_check_enabled,
                'id': hw.lesson.profession.pk,
            }
        hw_by_course[course_name]['tasks'].append(hw)
    
    sp_by_course = OrderedDict()
    for sp in speaking_tasks:
        course_name = sp.lesson.profession.name
        if course_name not in sp_by_course:
            sp_by_course[course_name] = {
                'tasks': [], 'ai_enabled': sp.lesson.profession.ai_check_enabled,
                'id': sp.lesson.profession.pk,
            }
        sp_by_course[course_name]['tasks'].append(sp)
    
    context = {
        'professions': professions,
        'hw_by_course': hw_by_course,
        'sp_by_course': sp_by_course,
        'total_hw': total_hw,
        'ai_on_hw': ai_on_hw,
        'ai_off_hw': ai_off_hw,
        'total_sp': total_sp,
        'ai_on_sp': ai_on_sp,
        'ai_off_sp': ai_off_sp,
        'selected_course': profession_id,
    }
    return render(request, 'accounts/admin/ai_checker.html', context)


@login_required
def admin_ai_checker_toggle(request):
    """AI tekşiruvçini yoqish/öchirish (AJAX)"""
    if not request.user.is_admin:
        return JsonResponse({'error': "Ruxsat yöq"}, status=403)
    
    if request.method != 'POST':
        return JsonResponse({'error': 'POST kerak'}, status=400)
    
    task_type = request.POST.get('type')  # 'homework' or 'speaking'
    task_id = request.POST.get('id')
    action = request.POST.get('action')  # 'on', 'off', 'all_on', 'all_off'
    
    if action in ('all_on', 'all_off'):
        new_val = action == 'all_on'
        profession_id = request.POST.get('profession_id', '')
        
        hw_qs = Homework.objects.all()
        sp_qs = SpeakingTask.objects.all()
        
        if profession_id:
            try:
                hw_qs = hw_qs.filter(lesson__profession_id=int(profession_id))
                sp_qs = sp_qs.filter(lesson__profession_id=int(profession_id))
            except (ValueError, TypeError):
                pass
        
        hw_updated = hw_qs.update(ai_check=new_val)
        sp_updated = sp_qs.update(ai_check=new_val)
        
        status_word = "yoqildi" if new_val else "öçirildi"
        return JsonResponse({
            'success': True,
            'message': f"Barça vazifalar AI tekşiruvi {status_word} ({hw_updated + sp_updated} ta)",
            'new_state': new_val,
        })
    
    try:
        task_id = int(task_id)
    except (ValueError, TypeError):
        return JsonResponse({'error': "Notöğri ID"}, status=400)
    
    if task_type == 'homework':
        obj = get_object_or_404(Homework, pk=task_id)
    elif task_type == 'speaking':
        obj = get_object_or_404(SpeakingTask, pk=task_id)
    else:
        return JsonResponse({'error': "Notöğri tur"}, status=400)
    
    obj.ai_check = action == 'on'
    obj.save(update_fields=['ai_check'])
    
    status_text = "Yoqildi" if obj.ai_check else "Öçirildi"
    return JsonResponse({
        'success': True,
        'new_state': obj.ai_check,
        'message': f"{status_text}: {obj.lesson.title}",
    })


@login_required
def admin_ai_course_toggle(request):
    """Kurs uçun AI tekşiruvçini yoqish/öchirish (Admin override)"""
    if not request.user.is_admin:
        return JsonResponse({'error': "Ruxsat yöq"}, status=403)
    
    if request.method != 'POST':
        return JsonResponse({'error': 'POST kerak'}, status=400)
    
    profession_id = request.POST.get('profession_id')
    try:
        profession = Profession.objects.get(pk=int(profession_id))
    except (Profession.DoesNotExist, ValueError, TypeError):
        return JsonResponse({'error': 'Kurs topilmadi'}, status=404)
    
    profession.ai_check_enabled = not profession.ai_check_enabled
    profession.save(update_fields=['ai_check_enabled'])
    
    status_text = "yoqildi" if profession.ai_check_enabled else "öçirildi"
    return JsonResponse({
        'success': True,
        'new_state': profession.ai_check_enabled,
        'message': f"'{profession.name}' kursi uçun AI tekşiruvçi {status_text}",
    })


@login_required
def admin_file_only_toggle(request):
    """Faqat fayl qabul qilişni yoqish/öchirish (AJAX)"""
    if not request.user.is_admin and request.user.role != 'teacher':
        return JsonResponse({'error': "Ruxsat yöq"}, status=403)

    if request.method != 'POST':
        return JsonResponse({'error': 'POST kerak'}, status=400)

    hw_id = request.POST.get('id')
    try:
        hw_id = int(hw_id)
    except (ValueError, TypeError):
        return JsonResponse({'error': "Notöğri ID"}, status=400)

    if request.user.is_admin:
        hw = get_object_or_404(Homework, pk=hw_id)
    else:
        professions = request.user.teaching_professions.all()
        hw = get_object_or_404(Homework, pk=hw_id, lesson__profession__in=professions)

    hw.file_only = not hw.file_only
    hw.save(update_fields=['file_only'])

    status_text = "yoqildi" if hw.file_only else "öçirildi"
    return JsonResponse({
        'success': True,
        'new_state': hw.file_only,
        'message': f"Faqat fayl rejimi {status_text}: {hw.lesson.title}",
    })


# ============================================
# SÖROVNOMALAR BOŞQARUVI (ADMIN)
# ============================================

def _survey_audience_count(target_role):
    """Sörovnoma möljallangan faol foydalanuvçilar soni."""
    users = CustomUser.objects.filter(is_active=True)
    if target_role in ('student', 'teacher'):
        users = users.filter(role=target_role)
    return users.count()


@login_required
def admin_surveys(request):
    if request.user.role != 'admin':
        return redirect('home')

    surveys = SurveyForm.objects.annotate(
        q_count=Count('questions', distinct=True),
        r_count=Count('responses', distinct=True),
    )

    audience = {
        'student': _survey_audience_count('student'),
        'teacher': _survey_audience_count('teacher'),
        'all': _survey_audience_count('all'),
    }

    survey_rows = []
    for survey in surveys:
        target = audience.get(survey.target_role, audience['all'])
        survey_rows.append({
            'obj': survey,
            'questions': survey.q_count,
            'responses': survey.r_count,
            'target': target,
            'percent': min(round(survey.r_count * 100 / target), 100) if target else 0,
        })

    context = {
        'survey_rows': survey_rows,
        'total_count': len(survey_rows),
        'active_count': sum(1 for row in survey_rows if row['obj'].is_active),
        'inactive_count': sum(1 for row in survey_rows if not row['obj'].is_active),
        'total_responses': SurveyResponse.objects.count(),
    }
    return render(request, 'accounts/admin/surveys/survey_list.html', context)


@login_required
def admin_survey_create(request):
    if request.user.role != 'admin':
        return redirect('home')
    
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        description = request.POST.get('description', '').strip()
        is_active = request.POST.get('is_active') == 'on'
        show_on_login = request.POST.get('show_on_login') == 'on'
        is_required = request.POST.get('is_required') == 'on'
        target_role = request.POST.get('target_role', 'all')
        
        if not title:
            messages.error(request, "Forma nomi kiritilişi şart! ⚠️")
            return render(request, 'accounts/admin/surveys/survey_form.html', {'mode': 'create'})
        
        survey = SurveyForm.objects.create(
            title=title,
            description=description,
            is_active=is_active,
            show_on_login=show_on_login,
            is_required=is_required,
            target_role=target_role,
        )
        
        # Savollarni saqlaş
        question_texts = request.POST.getlist('question_text')
        question_types = request.POST.getlist('question_type')
        question_choices = request.POST.getlist('question_choices')
        question_required = request.POST.getlist('question_required')
        
        for i, text in enumerate(question_texts):
            if text.strip():
                SurveyQuestion.objects.create(
                    survey=survey,
                    question_text=text.strip(),
                    question_type=question_types[i] if i < len(question_types) else 'text',
                    choices=question_choices[i].strip() if i < len(question_choices) else '',
                    is_required=str(i) in question_required,
                    order=i,
                )
        
        messages.success(request, f"'{survey.title}' sörovnomasi yaratildi! ✅")
        return redirect('admin_surveys')
    
    return render(request, 'accounts/admin/surveys/survey_form.html', {'mode': 'create'})


@login_required
def admin_survey_edit(request, pk):
    if request.user.role != 'admin':
        return redirect('home')
    
    survey = get_object_or_404(SurveyForm, pk=pk)
    
    if request.method == 'POST':
        survey.title = request.POST.get('title', '').strip()
        survey.description = request.POST.get('description', '').strip()
        survey.is_active = request.POST.get('is_active') == 'on'
        survey.show_on_login = request.POST.get('show_on_login') == 'on'
        survey.is_required = request.POST.get('is_required') == 'on'
        survey.target_role = request.POST.get('target_role', 'all')
        
        if not survey.title:
            messages.error(request, "Forma nomi kiritilişi şart! ⚠️")
            return render(request, 'accounts/admin/surveys/survey_form.html', {
                'mode': 'edit', 'survey': survey
            })
        
        survey.save()
        
        # Eski savollarni öçiriş va yangilarini saqlaş
        survey.questions.all().delete()
        
        question_texts = request.POST.getlist('question_text')
        question_types = request.POST.getlist('question_type')
        question_choices = request.POST.getlist('question_choices')
        question_required = request.POST.getlist('question_required')
        
        for i, text in enumerate(question_texts):
            if text.strip():
                SurveyQuestion.objects.create(
                    survey=survey,
                    question_text=text.strip(),
                    question_type=question_types[i] if i < len(question_types) else 'text',
                    choices=question_choices[i].strip() if i < len(question_choices) else '',
                    is_required=str(i) in question_required,
                    order=i,
                )
        
        messages.success(request, f"'{survey.title}' yangilandi! ✅")
        return redirect('admin_surveys')
    
    context = {
        'mode': 'edit',
        'survey': survey,
        'questions': survey.questions.all(),
    }
    return render(request, 'accounts/admin/surveys/survey_form.html', context)


@login_required
def admin_survey_delete(request, pk):
    if request.user.role != 'admin':
        return redirect('home')
    
    survey = get_object_or_404(SurveyForm, pk=pk)
    
    if request.method == 'POST':
        title = survey.title
        survey.delete()
        messages.success(request, f"'{title}' öçirildi! 🗑️")
        return redirect('admin_surveys')
    
    return render(request, 'accounts/admin/surveys/survey_delete.html', {'survey': survey})


def _survey_question_stats(questions, responses):
    """Har bir savol böyiça jamlanma statistika."""
    total = len(responses)
    grouped = {question.id: [] for question in questions}
    for response in responses:
        for answer in response.answers.all():
            if answer.question_id in grouped:
                grouped[answer.question_id].append((answer.answer_text or '').strip())

    stats = []
    for question in questions:
        filled = [text for text in grouped.get(question.id, []) if text]
        item = {
            'question': question,
            'type': question.question_type,
            'answered': len(filled),
            'skipped': max(total - len(filled), 0),
        }
        base = len(filled) or 1

        if question.question_type in ('radio', 'select', 'checkbox'):
            counter = {choice: 0 for choice in question.get_choices_list()}
            extra = {}
            for text in filled:
                picks = text.split(',') if question.question_type == 'checkbox' else [text]
                for pick in picks:
                    pick = pick.strip()
                    if not pick:
                        continue
                    if pick in counter:
                        counter[pick] += 1
                    else:
                        extra[pick] = extra.get(pick, 0) + 1

            ordered = list(counter.items()) + sorted(extra.items(), key=lambda kv: -kv[1])
            top = max((count for _, count in ordered), default=0)
            item['options'] = [
                {
                    'label': label,
                    'count': count,
                    'percent': round(count * 100 / base),
                    'is_top': bool(top) and count == top,
                }
                for label, count in ordered
            ]

        elif question.question_type == 'rating':
            dist = {star: 0 for star in range(1, 6)}
            score = 0
            for text in filled:
                try:
                    value = int(text)
                except (TypeError, ValueError):
                    continue
                if 1 <= value <= 5:
                    dist[value] += 1
                    score += value
            voted = sum(dist.values())
            average = round(score / voted, 1) if voted else 0
            item['voted'] = voted
            item['average'] = average
            item['stars'] = [star <= round(average) for star in range(1, 6)]
            item['rating_rows'] = [
                {
                    'star': star,
                    'count': dist[star],
                    'percent': round(dist[star] * 100 / (voted or 1)),
                }
                for star in range(5, 0, -1)
            ]

        else:
            item['quotes'] = filled[:6]
            item['more'] = max(len(filled) - 6, 0)

        stats.append(item)
    return stats


@login_required
def admin_survey_responses(request, pk):
    if not request.user.is_admin_or_assistant:
        return redirect('home')
    from .models import SurveyForm, SurveyResponse
    survey = get_object_or_404(SurveyForm, pk=pk)
    responses = list(
        SurveyResponse.objects.filter(survey=survey)
        .select_related('user')
        .prefetch_related('answers__question')
        .order_by('-submitted_at')
    )
    questions = list(survey.questions.all())
    target = _survey_audience_count(survey.target_role)

    return render(request, 'accounts/admin/surveys/survey_responses.html', {
        'survey': survey,
        'responses': responses,
        'questions': questions,
        'stats': _survey_question_stats(questions, responses),
        'response_count': len(responses),
        'target': target,
        'percent': min(round(len(responses) * 100 / target), 100) if target else 0,
    })


@login_required
def admin_survey_responses_excel(request, pk):
    if not request.user.is_admin_or_assistant:
        return redirect('home')

    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    survey = get_object_or_404(SurveyForm, pk=pk)
    questions = list(survey.questions.all())
    responses = survey.responses.select_related('user').prefetch_related('answers').order_by('-submitted_at')

    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "Javoblar"
    worksheet.freeze_panes = 'A2'

    headers = ["Ism", "Familiya", "Töldirgan vaqti"] + [
        question.question_text for question in questions
    ]
    header_fill = PatternFill(fill_type='solid', fgColor='6366F1')
    header_font = Font(color='FFFFFF', bold=True)

    for column, header in enumerate(headers, 1):
        cell = worksheet.cell(row=1, column=column, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

    def excel_safe(value):
        value = value or ''
        if isinstance(value, str) and value.startswith(('=', '+', '-', '@')):
            return "'" + value
        return value

    for row, survey_response in enumerate(responses, 2):
        answers = {
            answer.question_id: answer.answer_text or ''
            for answer in survey_response.answers.all()
        }
        submitted_at = timezone.localtime(survey_response.submitted_at).replace(tzinfo=None)
        values = [
            survey_response.user.first_name,
            survey_response.user.last_name,
            submitted_at,
            *[answers.get(question.pk, '') for question in questions],
        ]
        for column, value in enumerate(values, 1):
            cell = worksheet.cell(row=row, column=column, value=excel_safe(value))
            cell.alignment = Alignment(vertical='top', wrap_text=True)
        worksheet.cell(row=row, column=3).number_format = 'dd.mm.yyyy hh:mm'

    worksheet.auto_filter.ref = worksheet.dimensions
    for column, header in enumerate(headers, 1):
        max_length = max(
            len(str(worksheet.cell(row=row, column=column).value or ''))
            for row in range(1, worksheet.max_row + 1)
        )
        worksheet.column_dimensions[get_column_letter(column)].width = min(max(max_length + 2, 12), 50)

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    timestamp = timezone.now().strftime('%Y%m%d_%H%M')
    response['Content-Disposition'] = (
        f'attachment; filename="survey_{survey.pk}_responses_{timestamp}.xlsx"'
    )
    workbook.save(response)
    return response


@login_required
def admin_survey_toggle(request, pk):
    if request.user.role != 'admin':
        return redirect('home')
    from .models import SurveyForm
    survey = get_object_or_404(SurveyForm, pk=pk)
    survey.is_active = not survey.is_active
    survey.save(update_fields=['is_active'])
    state = "faollaştirildi" if survey.is_active else "töxtatildi"
    messages.success(request, f"'{survey.title}' {state}!")
    return redirect('admin_surveys')


@login_required
def fill_survey(request, pk):
    from .models import SurveyForm, SurveyQuestion, SurveyResponse, SurveyAnswer
    survey = get_object_or_404(SurveyForm, pk=pk, is_active=True)

    # Allaqaçon töldirilgan bölsa
    if SurveyResponse.objects.filter(survey=survey, user=request.user).exists():
        messages.info(request, "Siz bu sörovnomani allaqaçon töldirgansiz.")
        return redirect('home')

    questions = survey.questions.all()

    if request.method == 'POST':
        from django.db import transaction
        with transaction.atomic():
            response = SurveyResponse.objects.create(survey=survey, user=request.user)
            for question in questions:
                field_name = f'question_{question.id}'
                if question.question_type == 'checkbox':
                    values = request.POST.getlist(field_name)
                    answer_text = ', '.join(values)
                else:
                    answer_text = request.POST.get(field_name, '').strip()
                SurveyAnswer.objects.create(
                    response=response,
                    question=question,
                    answer_text=answer_text or None,
                )
        messages.success(request, "Javoblaringiz uçun rahmat! ✅")
        return redirect('home')

    return render(request, 'accounts/fill_survey.html', {
        'survey': survey, 'questions': questions,
    })


# ════════════════════════════════════════════════
#  BALANS TIZIMI VIEWS
# ════════════════════════════════════════════════

@login_required
def payment_wall(request):
    """
    Öquvçining tölov sahifasi; kerak bölsa balans orqali akkauntni oçadi.
    """
    from .models import StudentBalance, PaymentStatus, BalanceTopUp
    user = request.user

    # Tölov sahifasi barça öquvçilarga oçiq bölişi kerak. Oldingi
    # tekşiruv bloklanmagan öquvçini /tolov/ dan çiqarib yuborib,
    # online payment formiga yetib kelişiga yöl qöymas edi.
    if (not user.is_learner and not request.GET.get('preview')
            and request.resolver_match.url_name != 'student_payments'):
        messages.error(request, "Tölov sahifasi faqat öquvçilar uçun mavjud.")
        return redirect('home')

    # Balans va tölov holati
    balance, _ = StudentBalance.objects.get_or_create(user=user)
    ps, _ = PaymentStatus.objects.get_or_create(user=user)

    # Töliq yoki qisman tölov urinişi — POST orqali yoki avtomatik
    if request.method == 'POST' and request.POST.get('action') == 'pay':
        from django.db import transaction
        with transaction.atomic():
            if _auto_deduct_and_unblock(user):
                messages.success(request, "✅ Tölov muvaffaqiyatli amalga oşirildi! Akkauntingiz faollaştirildi.")
                return redirect('home')
            else:
                daily_min = int(round((ps.monthly_fee or 200000) / 30))
                messages.error(request, f"❌ Balans yetarli emas. Kamida 1 kunlik mablag' ({daily_min:,} söm) bölişi kerak.".replace(',', ' '))

    # Top-up tarixi (oxirgi 10)
    topups = BalanceTopUp.objects.filter(student=user).select_related('added_by')[:10]

    # Oylik tölov tarixi
    from .models import PaymentRecord
    pay_records = PaymentRecord.objects.filter(user=user).order_by('-year', '-month')[:12]
    from .models import TezCheckPaymentOrder
    online_orders = TezCheckPaymentOrder.objects.filter(student=user, purpose='monthly').order_by('-created_at')[:8]

    monthly_fee = int(ps.monthly_fee or 200000)
    half_month_fee = int(round(monthly_fee / 2))
    balance_amount = int(balance.amount)
    shortage = max(0, monthly_fee - balance_amount)
    has_partial_balance = balance_amount > 0 and shortage > 0
    today = timezone.localdate()
    days_late = max(0, (today - balance.next_payment_date).days) if balance.next_payment_date else 0
    grace_days = max(0, int(getattr(settings, 'TEZCHECK_LATE_GRACE_DAYS', 5)))

    from .models import calculate_extension_from_amount
    balance_preview_date, balance_days, _, balance_desc = calculate_extension_from_amount(
        balance.amount, monthly_fee, base_date=today
    )
    can_pay = balance_days > 0
    daily_fee = int(round(monthly_fee / 30))
    from kurslarim.access import purchased_course_ids
    from kurslarim.progress import course_state
    purchased_courses = []
    for course in Profession.objects.filter(pk__in=purchased_course_ids(user), is_blocked=False).order_by('name'):
        state = course_state(user, course)
        total = state['total_count']
        purchased_courses.append({
            'course': course,
            'percent': int(100 * state['done_count'] / total) if total else 0,
            'resume': state['resume'],
        })

    return render(request, 'accounts/payment_wall.html', {
        'purchased_courses': purchased_courses,
        'balance': balance,
        'ps': ps,
        'monthly_fee': monthly_fee,
        'half_month_fee': half_month_fee,
        'balance_amount': balance_amount,
        'shortage': shortage,
        'has_partial_balance': has_partial_balance,
        'topups': topups,
        'pay_records': pay_records,
        'online_orders': online_orders,
        'can_pay': can_pay,
        'balance_days': balance_days,
        'balance_desc': balance_desc,
        'balance_preview_date': balance_preview_date,
        'daily_fee': daily_fee,
        'days_late': days_late,
        'grace_days': grace_days,
        'grace_days_left': max(0, grace_days - days_late),
    })


def _balance_sweep_overdue():
    """
    Muddati ötgan (next_payment_date <= bugun) şartnoma emas öquvçilarning
    tölovini balansdan avtomatik yeçib, sanani keyingi oyga suradi.
    Şu tufayli "iyun" belgilangan sana öz-özidan "iyul"ga ötadi.
    Har bir jarayonda köp marta işlamasligi uçun cache-lock bilan çeklangan.
    """
    from django.core.cache import cache
    from .models import StudentBalance
    from datetime import date as _date

    if not cache.add('balance_sweep_lock', 1, 120):
        return
    today = _date.today()
    overdue = StudentBalance.objects.filter(
        is_contract=False,
        next_payment_date__isnull=False,
        next_payment_date__lte=today,
        user__role='student',
        user__is_finished=False,
    )
    charged_any = False
    for bal in overdue:
        try:
            before = bal.next_payment_date
            bal.advance_due_payment()
            bal.refresh_from_db()
            if bal.next_payment_date != before:
                charged_any = True
        except Exception:
            pass
    if charged_any:
        # Dashboard moliya keşini yangilaymiz — qarzdorlik darhol töğri çiqsin
        get_performance_cache().delete('admin_finance_stats')

    # Har kuni öquvçilarga tölov sanasidan 3, 2, 1 kun oldin Telegram botdan eslatma yuboriş
    try:
        from telegram_bot.utils import notify_payment_reminders
        notify_payment_reminders()
    except Exception:
        pass


def _balance_management_context(request):
    """
    Balans Boşqaruvi sahifasi uçun umumiy kontekst (admin + yordamçi).
    Filtr, saralaş va şartnoma körinişini boşqaradi.
    """
    from .models import StudentBalance, BalanceTopUp
    from django.db.models import Sum
    from datetime import date as _date, timedelta as _td

    today = _date.today()
    SOON_DAYS = 7  # şunça kun içida tölov -> "yaqinlaşgan"

    search = request.GET.get('q', '').strip()
    view = request.GET.get('view', 'active')      # 'active' | 'contract'
    flt = request.GET.get('filter', '')           # '', 'no_date', 'soon', 'overdue', 'blocked'

    # Muddati ötgan tölovlarni keyingi oyga suriş (iyun -> iyul)
    if view != 'contract':
        _balance_sweep_overdue()

    base = CustomUser.objects.filter(
        role='student', is_finished=False
    ).select_related('student_balance', 'payment_status')

    if view == 'contract':
        students_qs = base.filter(student_balance__is_contract=True)
    else:
        students_qs = base.exclude(student_balance__is_contract=True)

    if search:
        students_qs = students_qs.filter(
            Q(first_name__icontains=search) |
            Q(last_name__icontains=search) |
            Q(username__icontains=search) |
            Q(phone__icontains=search) |
            Q(student_id__icontains=search)
        )

    # Ensure StudentBalance exists for all students
    existing_bal_ids = set(StudentBalance.objects.values_list('user_id', flat=True))
    missing_bal = [s.pk for s in students_qs if s.pk not in existing_bal_ids]
    if missing_bal:
        StudentBalance.objects.bulk_create(
            [StudentBalance(user_id=pk) for pk in missing_bal], ignore_conflicts=True
        )
        students_qs = students_qs.all()

    students_list = list(students_qs)

    # Har bir studentga tölov holati (days_left / pay_state) qöşamiz
    for s in students_list:
        bal = getattr(s, 'student_balance', None)
        npd = bal.next_payment_date if bal else None
        if npd is None:
            s.days_left = None
            s.pay_state = 'nodate'
        else:
            dl = (npd - today).days
            s.days_left = dl
            if dl < 0:
                s.pay_state = 'overdue'
            elif dl <= SOON_DAYS:
                s.pay_state = 'soon'
            else:
                s.pay_state = 'ok'

    # Filtrlar (faqat active körinişida)
    if view != 'contract':
        if flt == 'no_date':
            students_list = [s for s in students_list if s.pay_state == 'nodate']
        elif flt == 'soon':
            students_list = [s for s in students_list if s.pay_state in ('soon', 'overdue')]
        elif flt == 'overdue':
            students_list = [s for s in students_list if s.pay_state == 'overdue']
        elif flt == 'blocked':
            students_list = [s for s in students_list if s.is_blocked]

    # Saralaş: sanasi belgilanmagan (0) va yaqin/ötgan sanalar TEPADA
    def _sort_key(s):
        grp = 0 if s.days_left is None else 1
        dl = 0 if s.days_left is None else s.days_left
        return (grp, dl, (s.first_name or '').lower(), (s.last_name or '').lower())
    students_list.sort(key=_sort_key)

    paginator = Paginator(students_list, 25)
    students = paginator.get_page(request.GET.get('page'))

    # Statistics (faqat faol öquvçilar)
    total_balance = StudentBalance.objects.filter(
        is_contract=False, user__role='student', user__is_finished=False
    ).aggregate(t=Sum('amount'))['t'] or 0

    # Şartnomalardan keladigan umumiy summa
    total_contract_sum = StudentBalance.objects.filter(
        is_contract=True, user__is_finished=False
    ).aggregate(t=Sum('contract_amount'))['t'] or 0

    topup_today = BalanceTopUp.objects.filter(
        created_at__date=today, operation='add'
    ).aggregate(t=Sum('amount'))['t'] or 0
    blocked_count = CustomUser.objects.filter(
        role='student', is_finished=False, is_blocked=True
    ).exclude(student_balance__is_contract=True).count()
    contract_count = StudentBalance.objects.filter(
        is_contract=True, user__is_finished=False
    ).count()

    # Filtr sanoqlari (chip yonidagi raqamlar)
    active_bals = StudentBalance.objects.filter(
        is_contract=False, user__role='student', user__is_finished=False
    )
    no_date_count = active_bals.filter(next_payment_date__isnull=True).count()
    soon_count = active_bals.filter(
        next_payment_date__isnull=False,
        next_payment_date__lte=today + _td(days=SOON_DAYS)
    ).count()
    overdue_count = active_bals.filter(
        next_payment_date__isnull=False,
        next_payment_date__lt=today
    ).count()

    recent_topups = BalanceTopUp.objects.select_related('student', 'added_by').order_by('-created_at')[:30]

    return {
        'students': students,
        'search': search,
        'view': view,
        'filter': flt,
        'total_balance': total_balance,
        'total_contract_sum': total_contract_sum,
        'total_all_sum': total_balance + total_contract_sum,
        'topup_today': topup_today,
        'blocked_count': blocked_count,
        'contract_count': contract_count,
        'no_date_count': no_date_count,
        'soon_count': soon_count,
        'overdue_count': overdue_count,
        'recent_topups': recent_topups,
    }


@login_required
def admin_balance_management(request):
    """
    Admin — öquvçilarga balans qöşiş va tarix köriş.
    Kursni tugatgan öquvçilar körinmaydi; şartnoma rejimidagilar alohida
    körinişda (view=contract) körinadi.
    """
    if not request.user.is_admin_or_assistant:
        return redirect('home')
    return render(request, 'accounts/admin/balance_management.html',
                  _balance_management_context(request))


@login_required
def yordamchi_balance_management(request):
    """
    Yordamçi admin uçun alohida Balans Boşqaruvi sahifasi.
    Xuddi admin_balance_management mantiqini bajaradi, lekin yordamçi panelida oçiladi.
    """
    if not request.user.is_admin_or_assistant:
        return redirect('home')
    return render(request, 'accounts/yordamchi/balance_management.html',
                  _balance_management_context(request))


@login_required
def admin_balance_topup(request, student_id):
    """
    Bitta öquvçiga balans qöşiş (AJAX yoki oddiy POST).
    """
    if not request.user.is_admin_or_assistant:
        from django.http import JsonResponse
        return JsonResponse({'error': 'Ruxsat yöq'}, status=403)

    from .models import StudentBalance, BalanceTopUp
    from django.db import transaction

    student = get_object_or_404(CustomUser, pk=student_id, role='student')
    # Pul operatsiyasi audit izi — IP soxtalaştirilmasligi kerak.
    ip = get_client_ip(request) or ''

    if request.method == 'POST':
        try:
            amount_raw = request.POST.get('amount', '0').replace(' ', '').replace(',', '')
            amount = int(float(amount_raw))
            if amount <= 0:
                raise ValueError("Notöğri miqdor")
            if amount > 50_000_000:
                raise ValueError("Maksimal miqdor 50,000,000 söm")
            note = request.POST.get('note', '').strip()[:500]
        except (ValueError, TypeError) as e:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                from django.http import JsonResponse
                return JsonResponse({'error': str(e)}, status=400)
            messages.error(request, f'Xatolik: {e}')
            return redirect('admin_balance_management')

        with transaction.atomic():
            balance, _ = StudentBalance.objects.select_for_update().get_or_create(user=student)
            balance.amount += amount
            balance.save(update_fields=['amount', 'updated_at'])

            topup = BalanceTopUp.objects.create(
                student=student,
                amount=amount,
                operation='add',
                added_by=request.user,
                note=note,
                ip_address=ip[:45] if ip else None,
            )
            # Premium öquvçiga töldirilgan summaning 7% i bonus tanga (PREMIUM_TOPUP_BONUS_PERCENT).
            from .premium_bonus import award_topup_bonus
            bonus = award_topup_bonus(student, amount, source='admin', reference=f'topup:{topup.pk}')

            # Bloklangan bölsa → unblock + fee yeçiş + next_payment_date örnatiş
            # Bloklanmagan lekin next_payment_date yöq bölsa ham → birinçi aktivatsiya
            auto_unblocked = False
            balance.refresh_from_db()
            if student.is_blocked:
                auto_unblocked = _auto_deduct_and_unblock(student)
            elif not balance.is_contract and not balance.next_payment_date:
                # Birinçi marta balans töldirildi — oylik tölovni yeçib, sanani belgilaymiz
                _auto_deduct_and_unblock(student)
            elif not balance.is_contract and balance.next_payment_date:
                # Muddati ötgan bölsa — balansdan yeçib keyingi oyga suramiz
                # (iyun -> iyul). Muddat kelmagan bölsa heç narsa qilmaydi.
                try:
                    balance.advance_due_payment()
                except Exception:
                    pass

        # Öquvçiga Telegram orqali hisob töldirilgani haqida xabar yuboramiz
        try:
            from telegram_bot.utils import notify_balance_topup
            balance.refresh_from_db()
            notify_balance_topup(student, amount, 'manual', balance_amount=balance.amount)
        except Exception:
            pass

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            from django.http import JsonResponse
            balance.refresh_from_db()
            next_date_fmt = balance.next_payment_date.strftime('%d.%m.%Y') if balance.next_payment_date else None
            return JsonResponse({
                'success': True,
                'new_balance': int(balance.amount),
                'new_balance_fmt': f"{int(balance.amount):,}".replace(',', ' '),
                'auto_unblocked': auto_unblocked,
                'student_name': student.full_name,
                'amount': amount,
                'amount_fmt': f"{amount:,}".replace(',', ' '),
                'next_payment_date': next_date_fmt,
                'premium_bonus_coins': bonus,
            })

        if bonus:
            messages.info(request, f"💎 Premium bonus: {student.full_name} ga {bonus:,} tanga qöşildi.".replace(',', ' '))
        if auto_unblocked:
            messages.success(request, f"✅ {student.full_name} ga {amount:,} söm qöşildi va unblock böldi!")
        else:
            messages.success(request, f"✅ {student.full_name} ga {amount:,} söm qöşildi.")
        return redirect('admin_balance_management')

    from django.http import JsonResponse
    return JsonResponse({'error': 'Faqat POST'}, status=405)


@login_required
def admin_balance_deduct(request, student_id):
    """Faqat admin öquvçi balansidan mablag' ayirişi mumkin."""
    from django.db import transaction
    from django.http import JsonResponse
    from .models import BalanceTopUp, StudentBalance

    if not request.user.is_admin:
        return JsonResponse({'error': "Faqat admin balansdan ayira oladi"}, status=403)
    if request.method != 'POST':
        return JsonResponse({'error': 'Faqat POST'}, status=405)

    student = get_object_or_404(CustomUser, pk=student_id, role='student')
    try:
        amount_raw = request.POST.get('amount', '0').replace(' ', '').replace(',', '')
        amount = int(float(amount_raw))
        if amount <= 0:
            raise ValueError("Notöğri miqdor")
        if amount > 50_000_000:
            raise ValueError("Maksimal miqdor 50,000,000 söm")
        note = request.POST.get('note', '').strip()[:500]
        if not note:
            raise ValueError("Ayiriş sababini kiriting")
    except (ValueError, TypeError) as exc:
        return JsonResponse({'error': str(exc)}, status=400)

    # Pul operatsiyasi audit izi — IP soxtalaştirilmasligi kerak.
    ip = get_client_ip(request) or ''

    with transaction.atomic():
        balance, _ = StudentBalance.objects.select_for_update().get_or_create(user=student)
        if balance.amount < amount:
            return JsonResponse({
                'error': (
                    "Balansda yetarli mablag' yöq. "
                    f"Joriy balans: {int(balance.amount):,} söm".replace(',', ' ')
                )
            }, status=400)

        balance.amount -= amount
        balance.save(update_fields=['amount', 'updated_at'])
        BalanceTopUp.objects.create(
            student=student,
            amount=amount,
            operation='deduct',
            added_by=request.user,
            note=note,
            ip_address=ip[:45] if ip else None,
        )

    return JsonResponse({
        'success': True,
        'new_balance': int(balance.amount),
        'new_balance_fmt': f"{int(balance.amount):,}".replace(',', ' '),
        'student_name': student.full_name,
        'amount': amount,
        'amount_fmt': f"{amount:,}".replace(',', ' '),
    })


@login_required
def admin_balance_history(request, student_id):
    """Öquvçining balans özgarişlari va oylik tölov tarixini köriş (AJAX)."""
    from django.http import JsonResponse
    if not request.user.is_admin_or_assistant:
        return JsonResponse({'error': "Ruxsat yöq"}, status=403)
    from .models import BalanceTopUp, StudentBalance, PaymentRecord, PaymentStatus

    student = get_object_or_404(CustomUser, pk=student_id)
    balance, _ = StudentBalance.objects.get_or_create(user=student)
    ps, _ = PaymentStatus.objects.get_or_create(user=student, defaults={'monthly_fee': 200000})

    # Balans töldirişlar (oxirgi 50 ta)
    topups = BalanceTopUp.objects.filter(student=student).select_related('added_by').order_by('-created_at')[:50]

    # Oylik tölovlar (PaymentRecord)
    records = PaymentRecord.objects.filter(user=student).order_by('-year', '-month')[:24]

    MONTH_NAMES = {
        1: 'Yanvar', 2: 'Fevral', 3: 'Mart', 4: 'Aprel',
        5: 'May', 6: 'Iyun', 7: 'Iyul', 8: 'Avgust',
        9: 'Sentyabr', 10: 'Oktyabr', 11: 'Noyabr', 12: 'Dekabr'
    }

    data = {
        'student': student.full_name,
        'balance': int(balance.amount),
        'balance_fmt': f"{int(balance.amount):,}".replace(',', ' '),
        'monthly_fee': int(ps.monthly_fee or 0),
        'monthly_fee_fmt': f"{int(ps.monthly_fee or 0):,}".replace(',', ' '),
        'is_contract': balance.is_contract,
        'contract_name': balance.contract_name or '',
        'contract_amount': int(balance.contract_amount or 0),
        'contract_amount_fmt': f"{int(balance.contract_amount or 0):,}".replace(',', ' '),
        'next_payment_date': balance.next_payment_date.strftime('%d.%m.%Y') if balance.next_payment_date else None,
        'topups': [
            {
                'amount': int(t.amount),
                'amount_fmt': f"{int(t.amount):,}".replace(',', ' '),
                'operation': t.operation,
                'operation_display': t.get_operation_display(),
                'added_by': t.added_by.full_name if t.added_by else 'Tizim',
                'note': t.note or '',
                'date': t.created_at.strftime('%d.%m.%Y %H:%M'),
                'ip': t.ip_address or '',
            }
            for t in topups
        ],
        'payment_records': [
            {
                'month': r.month,
                'year': r.year,
                'month_name': MONTH_NAMES.get(r.month, ''),
                'amount': int(r.amount),
                'amount_fmt': f"{int(r.amount):,}".replace(',', ' '),
                'is_paid': r.is_paid,
                'paid_amount': int(r.paid_amount) if r.paid_amount else 0,
                'paid_amount_fmt': f"{int(r.paid_amount):,}".replace(',', ' ') if r.paid_amount else '0',
                'paid_date': r.paid_date.strftime('%d.%m.%Y') if r.paid_date else None,
            }
            for r in records
        ],
    }
    return JsonResponse(data)


@login_required
def admin_balance_set_fee(request, student_id):
    """Öquvçining oylik tolov miqdori, rejim va keyingi tolov sanasini ozgartiriş (AJAX POST)."""
    from django.http import JsonResponse
    from django.views.decorators.http import require_POST
    if not request.user.is_admin_or_assistant:
        return JsonResponse({'error': "Ruxsat yoq"}, status=403)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST kerak'}, status=405)

    from .models import StudentBalance, PaymentStatus
    student = get_object_or_404(CustomUser, pk=student_id)
    balance, _ = StudentBalance.objects.get_or_create(user=student)
    ps, _ = PaymentStatus.objects.get_or_create(user=student, defaults={'monthly_fee': 200000})

    mode = request.POST.get('mode', 'amount')

    if mode == 'contract':
        contract_name = request.POST.get('contract_name', '').strip()[:150]
        contract_amount_raw = request.POST.get('contract_amount', '0').replace(' ', '').replace(',', '')
        try:
            contract_amount = int(float(contract_amount_raw))
            if contract_amount < 0:
                contract_amount = 0
        except (ValueError, TypeError):
            contract_amount = 0

        balance.is_contract = True
        balance.contract_name = contract_name
        balance.contract_amount = contract_amount
        balance.next_payment_date = None
        balance.save(update_fields=['is_contract', 'contract_name', 'contract_amount', 'next_payment_date', 'updated_at'])
        ps.save()
        # Şartnoma rejimidagi öquvçining oylik tölovga umuman aloqasi
        # bölmasligi kerak. Tölanmagan oylik yozuvlarini öçiramiz.
        from .models import PaymentRecord
        PaymentRecord.objects.filter(user=student, is_paid=False).delete()
        return JsonResponse({
            'success': True,
            'mode': 'contract',
            'contract_name': balance.contract_name,
            'contract_amount': int(balance.contract_amount),
            'contract_amount_fmt': f"{int(balance.contract_amount):,}".replace(',', ' '),
            'fee': 0,
        })

    # mode == 'amount'
    fee_raw = request.POST.get('fee') or request.POST.get('monthly_fee', '')
    if not fee_raw:
        return JsonResponse({'success': False, 'error': "Oylik tölov summasi kiritilmadi"}, status=400)

    try:
        fee = int(str(fee_raw).replace(' ', '').replace(',', ''))
        if fee < 0:
            raise ValueError
    except (ValueError, TypeError):
        return JsonResponse({'success': False, 'error': "Notöğri summa kiritildi"}, status=400)

    ps.monthly_fee = fee
    ps.save()

    # Update unpaid PaymentRecord for current month if exists
    from datetime import date
    today = date.today()
    from .models import PaymentRecord
    PaymentRecord.objects.filter(
        user=student, month=today.month, year=today.year, is_paid=False
    ).update(amount=fee)

    # Update next_payment_date if provided
    date_str = request.POST.get('next_payment_date', '').strip()
    if date_str:
        try:
            from datetime import datetime
            balance.next_payment_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            balance.is_contract = False
            balance.save(update_fields=['next_payment_date', 'is_contract'])
        except ValueError:
            pass
    else:
        balance.is_contract = False
        balance.save(update_fields=['is_contract'])

    next_date_fmt = balance.next_payment_date.strftime('%d.%m.%Y') if balance.next_payment_date else None

    return JsonResponse({
        'success': True,
        'mode': 'amount',
        'fee': fee,
        'fee_fmt': f"{fee:,}".replace(',', ' '),
        'next_payment_date': next_date_fmt,
    })


# ──────────────────────────────────────────────────────────────
#  SECURITY EVENTS DASHBOARD
# ──────────────────────────────────────────────────────────────
@login_required
def admin_security_events(request):
    """Xavfsizlik hodisalari paneli."""
    from django.core.paginator import Paginator
    from django.utils import timezone as tz
    from datetime import timedelta
    from .models import SecurityEvent

    if not request.user.is_admin:
        return redirect('home')

    qs = SecurityEvent.objects.select_related('user', 'resolved_by').all()

    # Filters
    severity_f   = request.GET.get('severity', '')
    event_type_f = request.GET.get('event_type', '')
    ip_f         = request.GET.get('ip', '').strip()
    resolved_f   = request.GET.get('resolved', '')

    if severity_f:
        qs = qs.filter(severity=severity_f)
    if event_type_f:
        qs = qs.filter(event_type=event_type_f)
    if ip_f:
        qs = qs.filter(ip_address__icontains=ip_f)
    if resolved_f == '1':
        qs = qs.filter(resolved=True)
    elif resolved_f == '0':
        qs = qs.filter(resolved=False)

    # Stats
    now = tz.now()
    all_qs = SecurityEvent.objects.all()
    from django.db.models import Count
    top_ips_qs = (
        all_qs.values('ip_address')
        .annotate(cnt=Count('id'))
        .order_by('-cnt')[:5]
    )
    stats = {
        'total':      all_qs.count(),
        'unresolved': all_qs.filter(resolved=False).count(),
        'critical':   all_qs.filter(severity='critical').count(),
        'high':       all_qs.filter(severity='high').count(),
        'last24':     all_qs.filter(created_at__gte=now - timedelta(hours=24)).count(),
        'top_ips':    list(top_ips_qs),
    }

    paginator  = Paginator(qs, 50)
    page_obj   = paginator.get_page(request.GET.get('page', 1))

    return render(request, 'accounts/admin/security_events.html', {
        'page_obj':      page_obj,
        'stats':         stats,
        'severity_f':    severity_f,
        'event_type_f':  event_type_f,
        'ip_f':          ip_f,
        'resolved_f':    resolved_f,
        'severity_choices':   SecurityEvent.SEVERITY_CHOICES,
        'event_type_choices': SecurityEvent.EVENT_TYPES,
    })


@login_required
def admin_security_block_ip(request):
    """IP manzilni bloklaş (AJAX POST)."""
    from django.http import JsonResponse
    if not request.user.is_admin:
        return JsonResponse({'error': 'Ruxsat yoq'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST kerak'}, status=405)

    ip = request.POST.get('ip', '').strip()
    if not ip:
        return JsonResponse({'error': 'IP kiritilmadi'}, status=400)

    # Simple: record a SecurityEvent for manual block log
    from .models import SecurityEvent
    SecurityEvent.objects.create(
        event_type='ip_blocked',
        severity='high',
        ip_address=ip,
        detail=f"Admin {request.user.username} tomonidan qölda bloklandi",
        user=request.user,
    )
    return JsonResponse({'success': True, 'ip': ip})


# ══════════════════════════════════════════════════════════════
#  YORDAMÇI ADMIN — OYLIK TÖLOVLAR
# ══════════════════════════════════════════════════════════════







# ============================================================
# ÇAT - Guruh çati, Şaxsiy çat, Inbox (sahifalar)
# ============================================================

def _chat_can_access_group(user, profession):
    """Kursga yozilgan öquvçi YOKI biriktirilgan öqituvçi YOKI admin."""
    if user.is_admin or user.is_admin_or_assistant:
        return True
    if profession.teachers.filter(pk=user.pk).exists():
        return True
    return CourseEnrollment.objects.filter(user=user, profession=profession).exists()


def _chat_is_admin(user):
    return bool(user.is_admin or user.is_admin_or_assistant or user.is_staff)


@login_required
def group_chat_view(request, pk):
    """Kurs (guruh) çat xonasi — unified Messenger-style sahifa."""
    profession = get_object_or_404(Profession, pk=pk)
    if not _chat_can_access_group(request.user, profession):
        messages.error(request, "Bu guruh çatiga ruxsatingiz yöq!")
        return redirect('profession_detail', pk=pk)

    members_count = CourseEnrollment.objects.filter(profession=profession).count()
    can_moderate = _chat_is_admin(request.user) or profession.teachers.filter(pk=request.user.pk).exists()
    return render(request, 'accounts/chat/messenger.html', {
        'initial_kind': 'group',
        'initial_id': profession.pk,
        'initial_group': profession,
        'members_count': members_count,
        'initial_can_moderate': can_moderate,
        'is_admin_global': _chat_is_admin(request.user),
        'initial_filter': 'all',
    })


@login_required
def direct_chat_view(request, pk):
    """Ikki foydalanuvçi örtasidagi şaxsiy çat xonasi — unified Messenger-style sahifa."""
    from accounts.api.chat_views import _online_sub

    other = get_object_or_404(CustomUser, pk=pk)
    if other.pk == request.user.pk:
        return redirect('chat_inbox')
    return render(request, 'accounts/chat/messenger.html', {
        'initial_kind': 'dm',
        'initial_id': other.pk,
        'initial_partner': other,
        'initial_partner_sub': _online_sub(other),
        'is_admin_global': _chat_is_admin(request.user),
        'initial_filter': 'all',
    })


@login_required
def chat_inbox_view(request):
    """Suhbatlar — unified Messenger-style sahifa (şaxsiy filtri faol)."""
    return render(request, 'accounts/chat/messenger.html', {
        'is_admin_global': _chat_is_admin(request.user),
        'initial_filter': 'dm',
    })


@login_required
def my_group_chats_view(request):
    """Guruh çatlari — unified Messenger-style sahifa (guruh filtri faol)."""
    return render(request, 'accounts/chat/messenger.html', {
        'is_admin_global': _chat_is_admin(request.user),
        'initial_filter': 'group',
    })


@login_required
def admin_group_chats(request):
    """Admin panel: barça guruh çatlari."""
    if not (request.user.is_admin or request.user.is_admin_or_assistant):
        return redirect('home')
    from django.db.models import Count as _Count
    professions = Profession.objects.annotate(
        msg_count=_Count('chat_messages', distinct=True),
        member_count=_Count('enrollments', distinct=True),
    ).order_by('name')
    return render(request, 'accounts/admin/group_chats.html', {
        'professions': professions,
    })


# Şaxsiy çat monitoring: admin/yordamchi-admin öz şaxsiy xabarlarini
# maxfiy saqlaydi — ular qatnaşgan suhbatlar röyxatda körinmaydi.
# Öquvçi va öqituvçilar örtasidagi barça şaxsiy suhbatlar (öquvçi<->öquvçi,
# öquvçi<->öqituvçi, öqituvçi<->öqituvçi) körsatiladi.
_PRIVATE_CHAT_ROLES = ('admin', 'assistant_admin')


@login_required
def admin_direct_chats(request):
    """Admin panel: öquvchi/öqituvchilar örtasidagi şaxsiy (1:1) suhbatlar röyxati.

    Faqat köriş uçun (investigatsiya) — admin bu yerdan xabar yubora yoki
    öçira olmaydi. Admin/yordamchi-admin iştirokidagi şaxsiy xabarlar
    maxfiy qoladi va bu röyxatda körsatilmaydi."""
    if not (request.user.is_admin or request.user.is_admin_or_assistant):
        return redirect('home')

    from django.db.models import Case, When, IntegerField, Max, F, Count as _Count
    from .models import DirectMessage

    search_query = request.GET.get('q', '').strip()
    active_filter = request.GET.get('f', 'all')  # all | today | week | deleted
    if active_filter not in ('all', 'today', 'week', 'deleted'):
        active_filter = 'all'

    # Eslatma: is_deleted böyiça FILTR YÖQ — foydalanuvçi öçirgan
    # xabarlar ham admin monitoringida körinib turişi kerak.
    pairs = (
        DirectMessage.objects
        .exclude(sender__role__in=_PRIVATE_CHAT_ROLES)
        .exclude(recipient__role__in=_PRIVATE_CHAT_ROLES)
        .annotate(
            u1=Case(
                When(sender_id__lt=F('recipient_id'), then=F('sender_id')),
                default=F('recipient_id'),
                output_field=IntegerField(),
            ),
            u2=Case(
                When(sender_id__lt=F('recipient_id'), then=F('recipient_id')),
                default=F('sender_id'),
                output_field=IntegerField(),
            ),
        )
        .values('u1', 'u2')
        .annotate(
            msg_count=_Count('id'),
            last_at=Max('created_at'),
            last_id=Max('id'),
            deleted_count=_Count('id', filter=Q(is_deleted=True)),
            edited_count=_Count('id', filter=Q(is_edited=True)),
        )
        .order_by('-last_at')
    )

    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=7)

    user_ids = set()
    for p in pairs:
        user_ids.add(p['u1'])
        user_ids.add(p['u2'])
    users_by_id = CustomUser.objects.in_bulk(user_ids)

    conversations = []
    for p in pairs:
        u1 = users_by_id.get(p['u1'])
        u2 = users_by_id.get(p['u2'])
        if not u1 or not u2:
            continue
        if active_filter == 'today' and (not p['last_at'] or p['last_at'] < today_start):
            continue
        if active_filter == 'week' and (not p['last_at'] or p['last_at'] < week_start):
            continue
        if active_filter == 'deleted' and not p['deleted_count']:
            continue
        if search_query:
            q = search_query.lower()
            haystack = f"{u1.full_name} {u1.username} {u2.full_name} {u2.username}".lower()
            if q not in haystack:
                continue
        conversations.append({
            'u1': u1, 'u2': u2,
            'msg_count': p['msg_count'], 'last_at': p['last_at'],
            'deleted_count': p['deleted_count'], 'edited_count': p['edited_count'],
            'last_id': p['last_id'],
        })

    paginator = Paginator(conversations, 25)
    page_obj = paginator.get_page(request.GET.get('page'))

    # Joriy sahifadagi suhbatlarning oxirgi xabarlarini BITTa sörovda oliş
    last_ids = [c['last_id'] for c in page_obj.object_list if c.get('last_id')]
    last_msgs = DirectMessage.objects.in_bulk(last_ids) if last_ids else {}
    for c in page_obj.object_list:
        lm = last_msgs.get(c.get('last_id'))
        c['last_msg'] = lm
        if lm:
            c['last_msg_sender_is_u1'] = (lm.sender_id == c['u1'].pk)

    # AI tahlil paneli + statistika kartalar uçun qöşimça ma'lumotlar
    from .chat_ai import ai_available
    base_msgs = (
        DirectMessage.objects
        .exclude(sender__role__in=_PRIVATE_CHAT_ROLES)
        .exclude(recipient__role__in=_PRIVATE_CHAT_ROLES)
    )
    stats = {
        'total_messages': base_msgs.count(),
        'today_messages': base_msgs.filter(created_at__gte=today_start).count(),
        'deleted_messages': base_msgs.filter(is_deleted=True).count(),
        'today_conversations': len({
            tuple(sorted((m['sender_id'], m['recipient_id'])))
            for m in base_msgs.filter(created_at__gte=today_start).values('sender_id', 'recipient_id')
        }),
    }

    return render(request, 'accounts/admin/direct_chats.html', {
        'page_obj': page_obj,
        'search_query': search_query,
        'active_filter': active_filter,
        'total_count': len(conversations),
        'stats': stats,
        'ai_available': ai_available(),
    })


@login_required
@ai_offload
def admin_direct_chats_ai(request):
    """AI Çat Tahlilçi — admin savoliga çatlar bazasidan javob (AJAX, POST).

    OpenAI kaliti bölsa function-calling agent işlaydi, bölmasa offline
    qoidaviy tahlilçi. Javob: {'answer', 'links', 'mode', 'tools_used', 'ok'}."""
    if not (request.user.is_admin or request.user.is_admin_or_assistant):
        return JsonResponse({'ok': False, 'answer': "Ruxsat yöq.", 'links': []}, status=403)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'answer': "Faqat POST sörovi qabul qilinadi.", 'links': []}, status=405)

    question = (request.POST.get('question') or '').strip()[:600]
    if len(question) < 3:
        return JsonResponse({'ok': False, 'answer': "Savol juda qisqa — aniqroq yozing.", 'links': []}, status=400)

    # Kontekst: oxirgi 4 ta savol-javob (frontend yuboradi)
    try:
        history = json.loads(request.POST.get('history') or '[]')
        if not isinstance(history, list):
            history = []
    except ValueError:
        history = []

    # Oddiy rate-limit: soatiga 40 ta sörov (admin boşiga)
    rl_key = f"chat_ai_rl:{request.user.pk}"
    used = cache.get(rl_key, 0)
    if used >= 40:
        return JsonResponse({
            'ok': False,
            'answer': "⏳ Soatlik AI sörovlar limitingiz tugadi (40 ta). Birozdan söng urinib köring.",
            'links': [],
        }, status=429)
    cache.set(rl_key, used + 1, 3600)

    from .chat_ai import analyze_chats
    try:
        result = analyze_chats(question, history)
    except Exception as e:
        return JsonResponse({
            'ok': False,
            'answer': "⚠️ " + safe_error(e, 'admin_direct_chats_ai'),
            'links': [],
        }, status=500)

    return JsonResponse(result)


@login_required
@ai_offload
def admin_users_ai(request):
    """AI Foydalanuvçi Boşqaruvçisi — foydalanuvçilar bazasiga savol va
    buyruqlar (bloklaş, blokdan çiqariş, ismlarni tuzatiş...) uçun
    AJAX endpoint (POST).

    OpenAI kaliti bölsa function-calling agent işlaydi, bölmasa offline
    qoidaviy yordamçi. Javob: {'answer', 'links', 'mode', 'tools_used', 'ok'}."""
    if not request.user.is_admin:
        return JsonResponse({'ok': False, 'answer': "Ruxsat yöq.", 'links': []}, status=403)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'answer': "Faqat POST sörovi qabul qilinadi.", 'links': []}, status=405)

    question = (request.POST.get('question') or '').strip()[:600]
    if len(question) < 3:
        return JsonResponse({'ok': False, 'answer': "Savol juda qisqa — aniqroq yozing.", 'links': []}, status=400)

    # Kontekst: oxirgi 4 ta savol-javob (frontend yuboradi)
    try:
        history = json.loads(request.POST.get('history') or '[]')
        if not isinstance(history, list):
            history = []
    except ValueError:
        history = []

    # Oddiy rate-limit: soatiga 40 ta sörov (admin boşiga)
    rl_key = f"users_ai_rl:{request.user.pk}"
    used = cache.get(rl_key, 0)
    if used >= 40:
        return JsonResponse({
            'ok': False,
            'answer': "⏳ Soatlik AI sörovlar limitingiz tugadi (40 ta). Birozdan söng urinib köring.",
            'links': [],
        }, status=429)
    cache.set(rl_key, used + 1, 3600)

    from .users_ai import analyze_users
    try:
        result = analyze_users(question, history)
    except Exception as e:
        return JsonResponse({
            'ok': False,
            'answer': "⚠️ " + safe_error(e, 'admin_users_ai'),
            'links': [],
        }, status=500)

    return JsonResponse(result)


@login_required
def admin_direct_chat_detail(request, u1_pk, u2_pk):
    """Admin panel: ikkita foydalanuvçi örtasidagi şaxsiy suhbatni köriş (faqat öqiş).

    Admin/yordamchi-admin iştirokidagi suhbatlarga kiriş taqiqlanadi (maxfiy)."""
    if not (request.user.is_admin or request.user.is_admin_or_assistant):
        return redirect('home')
    if u1_pk == u2_pk:
        return redirect('admin_direct_chats')

    from .models import DirectMessage

    u1 = get_object_or_404(CustomUser, pk=u1_pk)
    u2 = get_object_or_404(CustomUser, pk=u2_pk)
    if u1.role in _PRIVATE_CHAT_ROLES or u2.role in _PRIVATE_CHAT_ROLES:
        return redirect('admin_direct_chats')

    # is_deleted böyiça filtr YÖQ: öçirilgan xabarlar ham admin
    # monitoringida (maxsus belgi bilan) körsatiladi. Tahrir tarixi esa
    # DirectMessageEditHistory orqali prefetch qilinadi.
    thread = (
        DirectMessage.objects
        .filter(Q(sender=u1, recipient=u2) | Q(sender=u2, recipient=u1))
        .select_related('sender', 'recipient', 'reply_to', 'reply_to__sender')
        .prefetch_related('edit_history', 'reactions', 'reactions__user')
        .order_by('-created_at')
    )

    thread_stats = {
        'total': thread.count(),
        'deleted': thread.filter(is_deleted=True).count(),
        'edited': thread.filter(is_edited=True).count(),
    }

    paginator = Paginator(thread, 50)
    page_obj = paginator.get_page(request.GET.get('page'))
    messages_chrono = list(reversed(page_obj.object_list))

    return render(request, 'accounts/admin/direct_chat_detail.html', {
        'u1': u1, 'u2': u2,
        'page_obj': page_obj,
        'messages_chrono': messages_chrono,
        'thread_stats': thread_stats,
    })


# ==================== GURUHLAR & DARS JADVALI ====================

def _parse_days_from_request(request):
    """POST'dan tanlangan kunlarni ('days' massivi) öqib, bit-mask qaytaradi."""
    days = request.POST.getlist('days')
    mask = 0
    for d in days:
        try:
            d_int = int(d)
            if 1 <= d_int <= 6:
                mask |= (1 << (d_int - 1))
        except (TypeError, ValueError):
            continue
    return mask


def _parse_time(value):
    """'HH:MM' formatidagi qatorni time obyektiga aylantiradi."""
    from datetime import datetime as _dt
    if not value:
        return None
    try:
        return _dt.strptime(value.strip(), '%H:%M').time()
    except (ValueError, AttributeError):
        try:
            return _dt.strptime(value.strip(), '%H:%M:%S').time()
        except (ValueError, AttributeError):
            return None


def _parse_date(value):
    """'YYYY-MM-DD' -> date (notöğri bölsa None)."""
    from datetime import datetime as _dt
    try:
        return _dt.strptime((value or '').strip(), '%Y-%m-%d').date()
    except ValueError:
        return None


GROUP_MIN_MONTHS, GROUP_MAX_MONTHS = 1, 9


def _add_months(day, months):
    import calendar
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))


def _group_period_errors(start_date, end_date, required):
    errors = []
    if required and (not start_date or not end_date):
        errors.append("Guruhning boşlaniş va tugaş sanasini kiriting.")
    if start_date and end_date and end_date < start_date:
        errors.append("Tugaş sanasi boşlaniş sanasidan oldin bölişi mumkin emas.")
    elif start_date and end_date and not (
            _add_months(start_date, GROUP_MIN_MONTHS) <= end_date <= _add_months(start_date, GROUP_MAX_MONTHS)):
        errors.append(f"Guruh davomiyligi kamida {GROUP_MIN_MONTHS} oy, köpi bilan {GROUP_MAX_MONTHS} oy bölişi kerak.")
    if required and end_date and end_date < timezone.localdate():
        errors.append("Tugaş sanasi ötib ketgan — bunday guruh darhol arxivga tuşadi.")
    return errors


def _teacher_group_readonly(request):
    """Öqituvçi guruhni tahrirlay va öçira olmaydi (faqat admin)."""
    messages.error(request, "Guruhni tahrirlaş va öçiriş faqat admin uçun. Özgartiriş kerak bölsa, adminga murojaat qiling.")
    return redirect('teacher:study_groups')


def _study_group_panel_context(user):
    """Guruh sahifalarini foydalanuvçining öz panelida oçiş sozlamalari."""
    if user.role == 'teacher':
        return {
            'study_groups_base': 'teacher/base_study_groups.html',
            'study_groups_url': 'teacher:study_groups',
            'study_group_create_url': 'teacher:study_group_create',
            'study_group_edit_url': 'teacher:study_group_edit',
            'study_group_delete_url': 'teacher:study_group_delete',
            'lock_teacher': True,
        }
    if user.is_assistant_admin:
        return {
            'study_groups_base': 'accounts/yordamchi/base_yordamchi.html',
            'study_groups_url': 'yordamchi_study_groups',
            'study_group_create_url': 'yordamchi_study_group_create',
            'study_group_edit_url': 'yordamchi_study_group_edit',
            'study_group_delete_url': 'yordamchi_study_group_delete',
        }
    return {
        'study_groups_base': 'accounts/admin/base_admin.html',
        'study_groups_url': 'admin_study_groups',
        'study_group_create_url': 'admin_study_group_create',
        'study_group_edit_url': 'admin_study_group_edit',
        'study_group_delete_url': 'admin_study_group_delete',
    }


def _can_manage_study_groups(user):
    return user.is_admin_or_assistant or user.role == 'teacher'


def _study_group_scope(user):
    """Foydalanuvçi boşqara oladigan guruhlar, kurslar, öqituvçilar, öquvçilar.

    Öqituvçi faqat özi biriktirilgan guruhlarni, özi dars beradigan kurslarni
    va şu kurslarga yozilgan öquvçilarni köradi; guruh öqituvçisi — özi.
    """
    if user.role == 'teacher':
        professions = user.teaching_professions.all()
        return {
            'groups': StudyGroup.objects.filter(teacher=user),
            'professions': professions,
            'teachers': CustomUser.objects.filter(pk=user.pk),
            'students': CustomUser.objects.filter(
                role='student', is_blocked=False,
                enrollments__profession__in=professions,
            ).distinct().order_by('first_name'),
        }
    return {
        'groups': StudyGroup.objects.all(),
        'professions': Profession.objects.all(),
        'teachers': CustomUser.objects.filter(role='teacher').order_by('first_name'),
        'students': CustomUser.objects.filter(role='student', is_blocked=False).order_by('first_name'),
    }


def _teacher_study_group_redirect(request, url_name, **kwargs):
    """Öqituvçi admin URL orqali kirsa — öz panelidagi sahifaga."""
    if request.user.role == 'teacher' and request.resolver_match.namespace != 'teacher':
        target = reverse(f'teacher:{url_name}', kwargs=kwargs or None)
        if request.GET:
            target = f'{target}?{request.GET.urlencode()}'
        return redirect(target)
    return None


@login_required
def admin_study_groups(request):
    """Guruhlar & Dars jadvali röyxati."""
    if not _can_manage_study_groups(request.user):
        messages.error(request, "Sizda bu sahifaga kiriş huquqi yöq!")
        return redirect('home')
    teacher_redirect = _teacher_study_group_redirect(request, 'study_groups')
    if teacher_redirect:
        return teacher_redirect

    if request.user.is_assistant_admin and request.resolver_match.url_name == 'admin_study_groups':
        target = reverse('yordamchi_study_groups')
        if request.GET:
            target = f'{target}?{request.GET.urlencode()}'
        return redirect(target)

    q = request.GET.get('q', '').strip()
    profession_id = request.GET.get('profession', '')
    day_filter = request.GET.get('day', '')
    teacher_id = request.GET.get('teacher', '')
    view_mode = request.GET.get('view', 'list')  # list yoki schedule
    # current — faol + kelgusi (standart), active, upcoming, archived, all
    status_filter = request.GET.get('status', 'current')
    if status_filter not in ('current', 'active', 'upcoming', 'archived', 'all'):
        status_filter = 'current'

    scope = _study_group_scope(request.user)
    all_groups = scope['groups']
    today = timezone.localdate()
    status_counts = {
        'current': all_groups.not_archived().count(),
        'active': all_groups.running().count(),
        'upcoming': all_groups.not_archived().filter(start_date__gt=today).count(),
        'archived': all_groups.archived().count(),
        'all': all_groups.count(),
    }
    if status_filter == 'current':
        all_groups = all_groups.not_archived()
    elif status_filter == 'active':
        all_groups = all_groups.running()
    elif status_filter == 'upcoming':
        all_groups = all_groups.not_archived().filter(start_date__gt=today)
    elif status_filter == 'archived':
        all_groups = all_groups.archived()
    groups = all_groups.select_related('profession', 'teacher', 'created_by').prefetch_related('students')

    if q:
        groups = groups.filter(Q(name__icontains=q) | Q(profession__name__icontains=q))
    if profession_id:
        groups = groups.filter(profession_id=profession_id)
    if teacher_id:
        groups = groups.filter(teacher_id=teacher_id)
    if day_filter:
        try:
            d = int(day_filter)
            if 1 <= d <= 6:
                groups = groups.filter(days_mask__gte=1).extra(
                    where=[f'(days_mask & {1 << (d - 1)}) > 0']
                )
        except (TypeError, ValueError):
            pass

    groups = groups.order_by('start_time', 'name')

    # Haftalik jadvalda faqat hozir dars ötilayotgan guruhlar
    weekly_by_day = []
    running_groups = [g for g in groups if g.is_running]
    for d_num, d_name in StudyGroup.DAYS_OF_WEEK:
        day_groups = [g for g in running_groups if g.has_day(d_num)]
        day_groups.sort(key=lambda x: x.start_time)
        weekly_by_day.append({
            'day_num': d_num,
            'day_name': d_name,
            'lessons': day_groups,
            'is_today': d_num == timezone.localtime().isoweekday(),
        })

    professions = scope['professions']
    teachers = scope['teachers']

    context = {
        'groups': groups,
        'weekly_by_day': weekly_by_day,
        'days_of_week': StudyGroup.DAYS_OF_WEEK,
        'professions': professions,
        'teachers': teachers,
        'q': q,
        'selected_profession': profession_id,
        'selected_teacher': teacher_id,
        'selected_day': day_filter,
        'view_mode': view_mode,
        'total_groups': groups.count(),
        'active_groups': sum(1 for g in groups if g.is_running),
        'status_filter': status_filter,
        'status_counts': status_counts,
        'can_edit_groups': request.user.is_admin_or_assistant,
    }
    if request.user.role == 'teacher':
        # Öqituvçi faqat guruhsiz öquvçilarni guruhga qöşa oladi
        from .study_groups import ungrouped_students
        ungrouped = {}
        for g in groups:
            if g.status in ('active', 'upcoming'):
                if g.profession_id not in ungrouped:
                    ungrouped[g.profession_id] = list(ungrouped_students(g.profession))
                g.ungrouped = ungrouped[g.profession_id]
    context.update(_study_group_panel_context(request.user))
    return render(request, 'accounts/admin/study_groups.html', context)


@login_required
def admin_study_group_create(request):
    if not _can_manage_study_groups(request.user):
        messages.error(request, "Sizda bu sahifaga kiriş huquqi yöq!")
        return redirect('home')
    teacher_redirect = _teacher_study_group_redirect(request, 'study_group_create')
    if teacher_redirect:
        return teacher_redirect

    if request.user.is_assistant_admin and request.resolver_match.url_name == 'admin_study_group_create':
        return redirect('yordamchi_study_group_create')

    panel_context = _study_group_panel_context(request.user)
    scope = _study_group_scope(request.user)

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        profession_id = request.POST.get('profession')
        teacher_id = request.POST.get('teacher')
        start_time = _parse_time(request.POST.get('start_time'))
        end_time = _parse_time(request.POST.get('end_time'))
        room = request.POST.get('room', '').strip()
        color = request.POST.get('color', '#6366f1').strip() or '#6366f1'
        description = request.POST.get('description', '').strip()
        days_mask = _parse_days_from_request(request)
        student_ids = request.POST.getlist('students')
        start_date = _parse_date(request.POST.get('start_date'))
        end_date = _parse_date(request.POST.get('end_date'))

        errors = _group_period_errors(start_date, end_date, required=True)
        if not name:
            errors.append("Guruh nomi kiritilişi şart.")
        if not profession_id:
            errors.append("Kurs tanlang.")
        if not start_time or not end_time:
            errors.append("Dars vaqtini töğri kiriting.")
        if start_time and end_time and start_time >= end_time:
            errors.append("Tugaş vaqti boşlanişdan katta bölişi kerak.")
        if not days_mask:
            errors.append("Kamida bitta kun tanlang.")
        if profession_id and not scope['professions'].filter(pk=profession_id).exists():
            errors.append("Bu kurs uçun guruh oça olmaysiz.")
        if request.user.role == 'teacher':
            teacher_id = request.user.pk
        elif teacher_id and not scope['teachers'].filter(pk=teacher_id).exists():
            errors.append("Öqituvçi topilmadi.")

        if errors:
            for e in errors:
                messages.error(request, e)
        else:
            group = StudyGroup.objects.create(
                name=name,
                profession_id=profession_id,
                teacher_id=teacher_id or None,
                start_time=start_time,
                end_time=end_time,
                days_mask=days_mask,
                room=room,
                color=color,
                description=description,
                start_date=start_date,
                end_date=end_date,
                created_by=request.user,
            )
            if student_ids:
                allowed = scope['students']
                if request.user.role == 'teacher':
                    from .study_groups import ungrouped_students
                    allowed = ungrouped_students(group.profession, allowed)
                group.students.set(allowed.filter(pk__in=student_ids))
            messages.success(request, f"'{group.name}' guruhi yaratildi.")
            return redirect(panel_context['study_groups_url'])

    professions = scope['professions']
    teachers = scope['teachers']
    students = scope['students']

    if request.user.role == 'teacher':
        # Öqituvçiga faqat guruhsiz öquvçilar körinadi (qaysi kursda guruhsizligi bilan)
        from .study_groups import ungrouped_students
        by_student = {}
        for p in professions:
            for st in ungrouped_students(p, students):
                by_student.setdefault(st.pk, [st, []])[1].append(p.pk)
        students = []
        for st, prof_ids in by_student.values():
            st.prof_ids = ' '.join(map(str, prof_ids))
            students.append(st)
    else:
        students = _students_with_current_groups(students)

    context = {
        'professions': professions,
        'teachers': teachers,
        'students': students,
        'days_of_week': StudyGroup.DAYS_OF_WEEK,
        'group': None,
        'selected_days': [],
        'selected_students': [],
        'only_ungrouped': request.user.role == 'teacher',
        'today': timezone.localdate(),
    }
    context.update(panel_context)
    return render(request, 'accounts/admin/study_group_form.html', context)


@login_required
def admin_study_group_edit(request, pk):
    if not _can_manage_study_groups(request.user):
        messages.error(request, "Sizda bu sahifaga kiriş huquqi yöq!")
        return redirect('home')
    teacher_redirect = _teacher_study_group_redirect(request, 'study_group_edit', pk=pk)
    if teacher_redirect:
        return teacher_redirect

    if request.user.is_assistant_admin and request.resolver_match.url_name == 'admin_study_group_edit':
        return redirect('yordamchi_study_group_edit', pk=pk)
    if request.user.role == 'teacher':
        return _teacher_group_readonly(request)

    panel_context = _study_group_panel_context(request.user)
    scope = _study_group_scope(request.user)

    group = get_object_or_404(scope['groups'], pk=pk)

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        profession_id = request.POST.get('profession')
        teacher_id = request.POST.get('teacher')
        start_time = _parse_time(request.POST.get('start_time'))
        end_time = _parse_time(request.POST.get('end_time'))
        room = request.POST.get('room', '').strip()
        color = request.POST.get('color', '#6366f1').strip() or '#6366f1'
        description = request.POST.get('description', '').strip()
        days_mask = _parse_days_from_request(request)
        student_ids = request.POST.getlist('students')
        is_active = request.POST.get('is_active') == 'on'
        start_date = _parse_date(request.POST.get('start_date'))
        end_date = _parse_date(request.POST.get('end_date'))

        errors = _group_period_errors(start_date, end_date, required=False)
        if not name:
            errors.append("Guruh nomi kiritilişi şart.")
        if not profession_id:
            errors.append("Kurs tanlang.")
        if not start_time or not end_time:
            errors.append("Dars vaqtini töğri kiriting.")
        if start_time and end_time and start_time >= end_time:
            errors.append("Tugaş vaqti boşlanişdan katta bölişi kerak.")
        if not days_mask:
            errors.append("Kamida bitta kun tanlang.")
        if profession_id and not scope['professions'].filter(pk=profession_id).exists():
            errors.append("Bu kurs uçun guruh oça olmaysiz.")
        if request.user.role == 'teacher':
            teacher_id = request.user.pk
        elif teacher_id and not scope['teachers'].filter(pk=teacher_id).exists():
            errors.append("Öqituvçi topilmadi.")

        if errors:
            for e in errors:
                messages.error(request, e)
        else:
            group.name = name
            group.profession_id = profession_id
            group.teacher_id = teacher_id or None
            group.start_time = start_time
            group.end_time = end_time
            group.days_mask = days_mask
            group.room = room
            group.color = color
            group.description = description
            group.is_active = is_active
            group.start_date = start_date
            group.end_date = end_date
            group.save()
            group.students.set(scope['students'].filter(pk__in=student_ids))
            messages.success(request, f"'{group.name}' guruhi yangilandi.")
            return redirect(panel_context['study_groups_url'])

    professions = scope['professions']
    teachers = scope['teachers']
    students = scope['students']

    context = {
        'professions': professions,
        'teachers': teachers,
        'students': _students_with_current_groups(students),
        'days_of_week': StudyGroup.DAYS_OF_WEEK,
        'group': group,
        'selected_days': group.get_days_list(),
        'selected_students': list(group.students.values_list('pk', flat=True)),
    }
    context.update(panel_context)
    return render(request, 'accounts/admin/study_group_form.html', context)


@login_required
def admin_study_group_delete(request, pk):
    if not _can_manage_study_groups(request.user):
        messages.error(request, "Sizda bu sahifaga kiriş huquqi yöq!")
        return redirect('home')
    teacher_redirect = _teacher_study_group_redirect(request, 'study_group_delete', pk=pk)
    if teacher_redirect:
        return teacher_redirect
    if request.user.is_assistant_admin and request.resolver_match.url_name == 'admin_study_group_delete':
        return redirect('yordamchi_study_group_delete', pk=pk)
    if request.user.role == 'teacher':
        return _teacher_group_readonly(request)

    panel_context = _study_group_panel_context(request.user)
    group = get_object_or_404(_study_group_scope(request.user)['groups'], pk=pk)
    if request.method == 'POST':
        name = group.name
        group.delete()
        messages.success(request, f"'{name}' guruhi öçirildi.")
        return redirect(panel_context['study_groups_url'])
    return render(request, 'accounts/admin/study_group_delete.html', {
        'group': group,
        **panel_context,
    })


def _students_with_current_groups(students):
    """Admin formasi uçun: har bir öquvçining hozirgi (arxiv bölmagan) guruhlari — `cur_groups`."""
    from django.db.models import Prefetch
    return students.prefetch_related(Prefetch(
        'study_groups', queryset=StudyGroup.objects.not_archived().only('id', 'name', 'profession_id'),
        to_attr='cur_groups',
    ))


@login_required
def teacher_study_group_add_students(request, pk):
    """Öqituvçi: guruhsiz öquvçilarni öz guruhiga qöşadi (boşqa guruhdagini köçira olmaydi)."""
    from .study_groups import ungrouped_students

    if request.method != 'POST' or request.user.role != 'teacher':
        return redirect('teacher:study_groups')
    group = get_object_or_404(StudyGroup.objects.not_archived(), pk=pk, teacher=request.user)
    allowed = ungrouped_students(group.profession).filter(pk__in=request.POST.getlist('students'))
    added = list(allowed)
    if added:
        group.students.add(*added)
        messages.success(request, f"«{group.name}» guruhiga {len(added)} ta öquvçi qöşildi.")
    else:
        messages.info(request, "Qöşiladigan guruhsiz öquvçi tanlanmadi.")
    return redirect('teacher:study_groups')


# ==================== STUDENT & TEACHER — DARS JADVALIM ====================

def _build_weekly_schedule(groups_qs):
    """Guruhlar QuerySet'idan haftalik jadvalni list körinişida hosil qiladi.

    Har bir element: { 'day_num', 'day_name', 'lessons', 'is_today' }
    """
    current_day = timezone.localtime().isoweekday()
    result = []
    for d_num, d_name in StudyGroup.DAYS_OF_WEEK:
        day_groups = [g for g in groups_qs if g.has_day(d_num)]
        day_groups.sort(key=lambda x: x.start_time)
        result.append({
            'day_num': d_num,
            'day_name': d_name,
            'lessons': day_groups,
            'is_today': d_num == current_day,
        })
    return result


def _get_today_and_next_lesson(groups_qs):
    """Bugungi darslar va keyingi darsni topadi."""
    now = timezone.localtime()
    weekday_uz = now.isoweekday()  # 1=Du..7=Yakşanba

    today_lessons = []
    next_lesson = None

    for g in groups_qs:
        if g.has_day(weekday_uz):
            today_lessons.append(g)

    today_lessons.sort(key=lambda x: x.start_time)

    # Keyingi dars — bugundan boşlab
    now_time = now.time()
    for g in today_lessons:
        if g.start_time > now_time:
            next_lesson = g
            break

    # Bugun dars qolmagan bölsa — keyingi kunlardan qidiramiz
    if next_lesson is None:
        for offset in range(1, 8):
            check_day = ((weekday_uz - 1 + offset) % 7) + 1
            if check_day == 7:  # Yakşanba
                continue
            for g in groups_qs:
                if g.has_day(check_day):
                    if next_lesson is None or g.start_time < next_lesson.start_time:
                        next_lesson = g
            if next_lesson:
                break

    return today_lessons, next_lesson, weekday_uz


@login_required
def my_study_schedule(request):
    """Öquvçi yoki öqituvçining şaxsiy dars jadvali."""
    user = request.user
    if user.role == 'student':
        groups = StudyGroup.objects.running().filter(students=user)
        role_label = 'student'
    elif user.role == 'teacher':
        groups = StudyGroup.objects.running().filter(teacher=user)
        role_label = 'teacher'
    else:
        # Admin uçun ham körsatiladi, lekin admin panelga yönaltirilişi ma'qul
        return redirect('admin_study_groups')

    groups = groups.select_related('profession', 'teacher').prefetch_related('students').order_by('start_time')

    weekly = _build_weekly_schedule(groups)
    today_lessons, next_lesson, current_day = _get_today_and_next_lesson(groups)

    now = timezone.localtime()
    now_time = now.time()

    # Bugungi darslarni holatlar bilan boyitiş (kelayotgan/ketmoqda/tugagan + progress)
    today_lessons_enriched = []
    for g in today_lessons:
        if now_time < g.start_time:
            state = 'upcoming'
            progress = 0
        elif g.start_time <= now_time <= g.end_time:
            state = 'ongoing'
            start_min = g.start_time.hour * 60 + g.start_time.minute
            end_min = g.end_time.hour * 60 + g.end_time.minute
            now_min = now_time.hour * 60 + now_time.minute
            total = end_min - start_min
            elapsed = now_min - start_min
            progress = round(elapsed / total * 100) if total > 0 else 0
        else:
            state = 'ended'
            progress = 100
        today_lessons_enriched.append({
            'group': g,
            'state': state,
            'progress': progress,
        })

    # Keyingi darsgaça sanoq
    next_lesson_countdown = None
    if next_lesson:
        today_date = now.date()
        target_datetime = None
        if next_lesson in today_lessons and next_lesson.start_time > now_time:
            target_datetime = timezone.make_aware(
                timezone.datetime.combine(today_date, next_lesson.start_time)
            )
        else:
            for offset in range(1, 8):
                check_day = ((current_day - 1 + offset) % 7) + 1
                if check_day == 7:
                    continue
                if next_lesson.has_day(check_day):
                    check_date = today_date + timedelta(days=offset)
                    target_datetime = timezone.make_aware(
                        timezone.datetime.combine(check_date, next_lesson.start_time)
                    )
                    break
        if target_datetime and target_datetime > now:
            diff = target_datetime - now
            total_min = int(diff.total_seconds() // 60)
            next_lesson_countdown = {
                'days': total_min // (24 * 60),
                'hours': (total_min % (24 * 60)) // 60,
                'mins': total_min % 60,
                'total_min': total_min,
                'target_iso': target_datetime.isoformat(),
            }

    # Student uçun davomat statistikasi (oxirgi 30 kun)
    attendance_summary = None
    if role_label == 'student':
        month_ago = timezone.localdate() - timedelta(days=30)
        my_atts = StudentAttendance.objects.filter(student=user, date__gte=month_ago)
        total = my_atts.count()
        if total > 0:
            present = my_atts.filter(status__in=['present', 'late']).count()
            absent = my_atts.filter(status='absent').count()
            attendance_summary = {
                'total': total,
                'present': present,
                'absent': absent,
                'pct': round(present / total * 100),
            }

    context = {
        'groups': groups,
        'weekly': weekly,
        'days_of_week': StudyGroup.DAYS_OF_WEEK,
        'today_lessons': today_lessons,
        'today_lessons_enriched': today_lessons_enriched,
        'next_lesson': next_lesson,
        'next_lesson_countdown': next_lesson_countdown,
        'current_day': current_day,
        'role_label': role_label,
        'total_groups': groups.count(),
        'attendance_summary': attendance_summary,
        'now_time': now_time,
    }
    if role_label == 'teacher':
        template_name = 'teacher/my_study_schedule.html'
    else:
        template_name = 'accounts/my_study_schedule.html'
    return render(request, template_name, context)


# ==================== ÖQUVÇILAR DAVOMATI ====================

def _can_mark_attendance(user, group):
    """Foydalanuvçi guruh uçun davomat qöyişi mumkinmi?

    - Admin/Assistant admin: hamma guruhda
    - Teacher: faqat özi biriktirilgan guruhda
    """
    if not user.is_authenticated:
        return False
    if user.is_admin_or_assistant:
        return True
    if user.role == 'teacher' and group.teacher_id == user.pk:
        return True
    return False


@login_required
def attendance_home(request):
    """Davomat boş sahifasi — foydalanuvçi bemalol guruh tanlaydi va davomat qiladi."""
    user = request.user
    if not (user.is_admin_or_assistant or user.role == 'teacher'):
        messages.error(request, "Sizda bu sahifaga kiriş huquqi yöq!")
        return redirect('home')

    # Davomatda faqat boşlaniş va tugaş sanasi orasidagi (faol) guruhlar; arxiv körinmaydi
    if user.role == 'teacher':
        groups = StudyGroup.objects.running().filter(teacher=user)
    else:
        groups = StudyGroup.objects.running()

    groups = groups.select_related('profession', 'teacher').prefetch_related('students').order_by('name')

    today = timezone.localdate()
    weekday_today = today.isoweekday()

    # Bugun dars bölgan guruhlar
    today_groups = [g for g in groups if g.has_day(weekday_today)]

    # Umumiy statistika (oxirgi 7 kun)
    week_ago = today - timedelta(days=7)
    recent_attendances = StudentAttendance.objects.filter(
        date__gte=week_ago,
        group__in=groups,
    )
    total_marked = recent_attendances.count()
    present_pct = 0
    if total_marked > 0:
        present_count = recent_attendances.filter(status='present').count()
        present_pct = round(present_count / total_marked * 100, 1)

    # Har bir guruh uçun bugungi davomat uşbu holati
    group_status_map = {}
    for g in groups:
        today_att = StudentAttendance.objects.filter(group=g, date=today)
        marked = today_att.count()
        total = g.students.count()
        group_status_map[g.pk] = {
            'marked': marked,
            'total': total,
            'done': marked >= total and total > 0,
            'has_lesson_today': g.has_day(weekday_today),
        }

    context = {
        'groups': groups,
        'today_groups': today_groups,
        'today': today,
        'weekday_today': weekday_today,
        'group_status_map': group_status_map,
        'total_groups': groups.count(),
        'total_marked_week': total_marked,
        'present_pct': present_pct,
        'can_edit_all': user.is_admin_or_assistant,
    }
    if user.role == 'teacher':
        template_name = 'accounts/attendance/home.html'
    elif user.role == 'assistant_admin':
        template_name = 'accounts/yordamchi/attendance_home.html'
    else:
        template_name = 'accounts/admin/attendance_home.html'
    return render(request, template_name, context)


@login_required
def attendance_mark(request, group_pk):
    """Guruh uçun bir sana davomatini belgilaş — oson bir sahifada."""
    user = request.user
    group = get_object_or_404(StudyGroup, pk=group_pk)

    if not _can_mark_attendance(user, group):
        messages.error(request, "Sizda bu guruh uçun davomat qöyiş huquqi yöq!")
        return redirect('attendance_home')
    # Arxiv / hali boşlanmagan guruhga öqituvçi davomat qöya olmaydi (admin tuzatiş uçun kira oladi)
    if not user.is_admin_or_assistant and not group.is_running:
        messages.error(request, f"«{group.name}» guruhi {group.status_label.lower()} — davomat faqat faol guruhlarda qöyiladi.")
        return redirect('attendance_home')

    # Sana - GET dan yoki bugungi
    date_str = request.GET.get('date') or request.POST.get('date')
    if date_str:
        try:
            selected_date = timezone.datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            selected_date = timezone.localdate()
    else:
        selected_date = timezone.localdate()
    if not group.date_in_period(selected_date):
        messages.error(request, f"Bu sana guruh davriga ({group.period_display}) kirmaydi.")
        return redirect('attendance_home')

    students = list(group.students.filter(is_blocked=False).order_by('first_name', 'last_name'))
    existing = {a.student_id: a for a in StudentAttendance.objects.filter(group=group, date=selected_date)}

    if request.method == 'POST':
        saved = 0
        for s in students:
            status = request.POST.get(f'status_{s.pk}', '').strip()
            note = request.POST.get(f'note_{s.pk}', '').strip()
            if status not in dict(StudentAttendance.STATUS_CHOICES):
                continue
            att, created = StudentAttendance.objects.update_or_create(
                group=group, student=s, date=selected_date,
                defaults={
                    'status': status,
                    'note': note[:300],
                    'marked_by': user,
                },
            )
            saved += 1
        messages.success(request, f"{saved} ta öquvçi davomati saqlandi.")
        return redirect(f"{reverse('attendance_mark', args=[group.pk])}?date={selected_date.strftime('%Y-%m-%d')}")

    # Statistika
    total = len(students)
    stats = {'present': 0, 'absent': 0, 'late': 0, 'sick': 0, 'excused': 0, 'unmarked': 0}
    for s in students:
        att = existing.get(s.pk)
        if att:
            stats[att.status] = stats.get(att.status, 0) + 1
        else:
            stats['unmarked'] += 1

    # Har bir öquvçining oxirgi 30 kunlik statistikasi
    month_ago = selected_date - timedelta(days=30)
    student_month_stats = {}
    month_atts = StudentAttendance.objects.filter(
        group=group, date__gte=month_ago, date__lte=selected_date
    )
    for a in month_atts:
        m = student_month_stats.setdefault(a.student_id, {'present': 0, 'absent': 0, 'late': 0, 'sick': 0, 'excused': 0, 'total': 0})
        m[a.status] = m.get(a.status, 0) + 1
        m['total'] += 1

    # Sana navigatsiya
    prev_date = selected_date - timedelta(days=1)
    next_date = selected_date + timedelta(days=1)

    # Oxirgi 7 kun sanoq (button'lar uçun)
    recent_days = []
    for i in range(7):
        d = timezone.localdate() - timedelta(days=i)
        recent_days.append({
            'date': d,
            'label': d.strftime('%d.%m'),
            'weekday': d.isoweekday(),
            'is_selected': d == selected_date,
            'is_today': d == timezone.localdate(),
        })
    recent_days.reverse()

    # Şu kuni saytga kirganlar (öqituvçi belgisi bilan birga körinsin)
    from .models import StudentDailyVisit
    from .attendance_auto import AUTO_NOTE
    visits = dict(StudentDailyVisit.objects.filter(
        user__in=students, date=selected_date).values_list('user_id', 'first_seen'))

    students_with_status = []
    for s in students:
        att = existing.get(s.pk)
        m = student_month_stats.get(s.pk, {'present': 0, 'absent': 0, 'late': 0, 'sick': 0, 'excused': 0, 'total': 0})
        students_with_status.append({
            'student': s,
            'visited_at': visits.get(s.pk),
            'auto_marked': bool(att and att.note == AUTO_NOTE),
            'current_status': att.status if att else '',
            'current_note': att.note if att else '',
            'month_stats': m,
            'attendance_pct': round((m['present'] + m['late']) / m['total'] * 100) if m['total'] > 0 else None,
        })

    context = {
        'group': group,
        'selected_date': selected_date,
        'students_with_status': students_with_status,
        'stats': stats,
        'total_students': total,
        'status_choices': StudentAttendance.STATUS_CHOICES,
        'prev_date': prev_date,
        'next_date': next_date,
        'today': timezone.localdate(),
        'recent_days': recent_days,
        'weekday_name': dict(StudyGroup.DAYS_OF_WEEK).get(selected_date.isoweekday(), ''),
    }
    if user.role == 'teacher':
        template_name = 'accounts/attendance/mark.html'
    elif user.role == 'assistant_admin':
        template_name = 'accounts/yordamchi/attendance_mark.html'
    else:
        template_name = 'accounts/admin/attendance_mark.html'
    return render(request, template_name, context)


def _build_attendance_report_data(group, year, month):
    """Davomat sahifasi va Excel eksporti uçun bir xil oylik hisobotni tayyorlaydi."""
    today = timezone.localdate()
    if month < 1 or month > 12:
        month = today.month
    if year < 1 or year > 9999:
        year = today.year

    from calendar import monthrange
    _, days_in_month = monthrange(year, month)

    students = list(group.students.filter(is_blocked=False).order_by('first_name', 'last_name'))

    # Faqat guruh dars kunlarini oliş (masalan, du, ç, ju bölsa faqat şular)
    day_dates = []
    for d in range(1, days_in_month + 1):
        try:
            check_date = date(year, month, d)
            if group.has_day(check_date.isoweekday()):
                day_dates.append(check_date)
        except ValueError:
            pass

    # Attendance ma'lumotlarini oliş
    attendances = StudentAttendance.objects.filter(
        group=group, date__year=year, date__month=month
    ).select_related('student')

    att_map = {}  # (student_id, date) -> attendance
    for a in attendances:
        att_map[(a.student_id, a.date)] = a

    # Har bir öquvçi uçun jadval
    rows = []
    for s in students:
        cells = []
        totals = {'present': 0, 'absent': 0, 'late': 0, 'sick': 0, 'excused': 0, 'marked': 0}
        for d in day_dates:
            a = att_map.get((s.pk, d))
            if a:
                totals[a.status] = totals.get(a.status, 0) + 1
                totals['marked'] += 1
            cells.append({
                'date': d,
                'attendance': a,
            })
        # Ballli hisoblaş: present + late = 100, sick/excused=80, absent=0
        score_total = 0
        for d in day_dates:
            a = att_map.get((s.pk, d))
            if a:
                if a.status == 'present':
                    score_total += 100
                elif a.status == 'late':
                    score_total += 70
                elif a.status in ('sick', 'excused'):
                    score_total += 50
        avg_score = round(score_total / len(day_dates)) if day_dates else 0

        rows.append({
            'student': s,
            'cells': cells,
            'totals': totals,
            'avg_score': avg_score,
        })

    month_choices = [
        (1, 'Yanvar'), (2, 'Fevral'), (3, 'Mart'), (4, 'Aprel'),
        (5, 'May'), (6, 'Iyun'), (7, 'Iyul'), (8, 'Avgust'),
        (9, 'Sentabr'), (10, 'Oktabr'), (11, 'Noyabr'), (12, 'Dekabr'),
    ]
    return {
        'year': year,
        'month': month,
        'month_name': dict(month_choices).get(month, ''),
        'day_dates': day_dates,
        'rows': rows,
        'year_choices': list(range(today.year, today.year - 4, -1)),
        'month_choices': month_choices,
        'total_students': len(students),
    }


@login_required
def attendance_report(request, group_pk):
    """Guruh davomati böyiça oy/yil böyiça hisobot."""
    user = request.user
    group = get_object_or_404(StudyGroup, pk=group_pk)

    if not _can_mark_attendance(user, group):
        messages.error(request, "Sizda bu sahifaga kiriş huquqi yöq!")
        return redirect('attendance_home')

    today = timezone.localdate()
    year = _to_int(request.GET.get('year'), today.year)
    month = _to_int(request.GET.get('month'), today.month)
    context = {
        'group': group,
        **_build_attendance_report_data(group, year, month),
    }
    if user.role == 'teacher':
        template_name = 'accounts/attendance/report.html'
    elif user.role == 'assistant_admin':
        template_name = 'accounts/yordamchi/attendance_report.html'
    else:
        template_name = 'accounts/admin/attendance_report.html'
    return render(request, template_name, context)


@login_required
def attendance_report_excel(request, group_pk):
    """Admin va yordamçi admin uçun guruh davomatini Excel körinişida yuklaş."""
    user = request.user
    group = get_object_or_404(
        StudyGroup.objects.select_related('profession', 'teacher'),
        pk=group_pk,
    )
    if not user.is_admin_or_assistant:
        messages.error(request, "Excel hisobotini yuklab oliş huquqingiz yöq!")
        return redirect('attendance_report', group_pk=group.pk)

    today = timezone.localdate()
    year = _to_int(request.GET.get('year'), today.year)
    month = _to_int(request.GET.get('month'), today.month)
    report = _build_attendance_report_data(group, year, month)

    import openpyxl
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Davomat'
    ws.sheet_view.showGridLines = False

    headers = ["№", "Öquvçi"]
    headers.extend(d.strftime('%d.%m') for d in report['day_dates'])
    headers.extend(['Kelgan', 'Kelmagan', 'Keçikkan', 'Kasal', 'Sababli', 'Ball (%)'])
    last_column = get_column_letter(len(headers))

    ws.merge_cells(f'A1:{last_column}1')
    ws['A1'] = f"{group.name} — öquvçilar davomati"
    ws['A1'].font = Font(size=16, bold=True, color='FFFFFF')
    ws['A1'].fill = PatternFill('solid', fgColor='4F46E5')
    ws['A1'].alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[1].height = 30

    teacher_name = 'Biriktirilmagan'
    if group.teacher:
        teacher_name = group.teacher.get_full_name() or group.teacher.username
    ws.merge_cells(f'A2:{last_column}2')
    ws['A2'] = (
        f"Kurs: {group.profession.name} | Öqituvçi: {teacher_name} | "
        f"Davr: {report['month_name']} {report['year']} | "
        f"Öquvçilar: {report['total_students']}"
    )
    ws['A2'].font = Font(italic=True, color='475569')
    ws['A2'].alignment = Alignment(horizontal='center')

    header_row = 4
    header_fill = PatternFill('solid', fgColor='0F766E')
    header_font = Font(bold=True, color='FFFFFF')
    thin = Side(style='thin', color='CBD5E1')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for column, value in enumerate(headers, 1):
        cell = ws.cell(row=header_row, column=column, value=value)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.border = border

    status_fills = {
        'present': PatternFill('solid', fgColor='DCFCE7'),
        'absent': PatternFill('solid', fgColor='FEE2E2'),
        'late': PatternFill('solid', fgColor='FEF3C7'),
        'sick': PatternFill('solid', fgColor='F3E8FF'),
        'excused': PatternFill('solid', fgColor='DBEAFE'),
    }
    status_labels = dict(StudentAttendance.STATUS_CHOICES)
    first_data_row = header_row + 1
    for row_number, row in enumerate(report['rows'], first_data_row):
        student = row['student']
        student_name = student.get_full_name() or student.username
        values = [row_number - header_row, student_name]
        values.extend(
            status_labels.get(cell['attendance'].status, '') if cell['attendance'] else ''
            for cell in row['cells']
        )
        values.extend([
            row['totals']['present'],
            row['totals']['absent'],
            row['totals']['late'],
            row['totals']['sick'],
            row['totals']['excused'],
            row['avg_score'],
        ])
        for column, value in enumerate(values, 1):
            cell = ws.cell(row=row_number, column=column, value=value)
            cell.border = border
            cell.alignment = Alignment(
                horizontal='left' if column == 2 else 'center',
                vertical='center',
                wrap_text=True,
            )

        for offset, attendance_cell in enumerate(row['cells'], 3):
            attendance = attendance_cell['attendance']
            if attendance:
                ws.cell(row=row_number, column=offset).fill = status_fills[attendance.status]
                if attendance.note:
                    ws.cell(row=row_number, column=offset).comment = Comment(attendance.note, 'LMS')

    ws.freeze_panes = 'C5'
    ws.auto_filter.ref = f'A4:{last_column}{max(header_row, ws.max_row)}'
    ws.column_dimensions['A'].width = 6
    ws.column_dimensions['B'].width = 30
    for column in range(3, 3 + len(report['day_dates'])):
        ws.column_dimensions[get_column_letter(column)].width = 13
    for column in range(3 + len(report['day_dates']), len(headers) + 1):
        ws.column_dimensions[get_column_letter(column)].width = 12
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.fitToWidth = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = (
        f'attachment; filename="davomat_guruh_{group.pk}_{report["year"]}_{report["month"]:02d}.xlsx"'
    )
    wb.save(response)
    return response
