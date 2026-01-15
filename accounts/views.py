from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponse
from django.db.models import Count, Q
from django.db import models
from django.utils import timezone
from .forms import RegisterForm, LoginForm, ProfessionForm, UserProfileForm, AdminUserEditForm, ChangePasswordForm
from .models import Profession, CustomUser


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
        form = LoginForm(request, data=request.POST)
        if form.is_valid():
            username = form.cleaned_data.get('username')
            password = form.cleaned_data.get('password')
            user = authenticate(username=username, password=password)
            if user is not None:
                if user.is_blocked:
                    messages.error(request, "Sizning akkauntingiz bloklangan!")
                    return render(request, 'accounts/login.html', {'form': form})
                login(request, user)
                user.last_activity = timezone.now()
                user.save()
                messages.success(request, "Tizimga muvaffaqiyatli kirdingiz!")
                return redirect('home')
        else:
            messages.error(request, "Username yoki parol noto'g'ri!")
    else:
        form = LoginForm()
    
    return render(request, 'accounts/login.html', {'form': form})


def logout_view(request):
    logout(request)
    messages.success(request, "Tizimdan chiqdingiz!")
    return redirect('login')


@login_required
def home_view(request):
    return render(request, 'accounts/home.html')


@login_required
def professions_view(request):
    professions = Profession.objects.all()
    return render(request, 'accounts/professions.html', {'professions': professions})


@login_required
def profile_view(request):
    return render(request, 'accounts/profile.html')


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
            old_password = form.cleaned_data['old_password']
            new_password = form.cleaned_data['new_password1']
            
            if not request.user.check_password(old_password):
                messages.error(request, "Joriy parol noto'g'ri!")
            else:
                request.user.set_password(new_password)
                request.user.save()
                login(request, request.user)
                messages.success(request, "Parol muvaffaqiyatli o'zgartirildi!")
                return redirect('profile')
    else:
        form = ChangePasswordForm()
    
    return render(request, 'accounts/change_password.html', {'form': form})


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
    
    # Yo'nalishlar bo'yicha statistika
    profession_stats = Profession.objects.annotate(
        student_count=Count('students', filter=models.Q(students__role='student')),
        teacher_count=Count('students', filter=models.Q(students__role='teacher'))
    )
    
    # Oxirgi faol foydalanuvchilar
    recent_users = CustomUser.objects.filter(last_activity__isnull=False).order_by('-last_activity')[:10]
    
    context = {
        'total_users': total_users,
        'total_teachers': total_teachers,
        'total_students': total_students,
        'total_professions': total_professions,
        'blocked_users': blocked_users,
        'online_users': online_users,
        'profession_stats': profession_stats,
        'recent_users': recent_users,
    }
    return render(request, 'accounts/admin/dashboard.html', context)


@login_required
def admin_professions(request):
    if not request.user.is_admin:
        messages.error(request, "Sizda admin paneliga kirish huquqi yo'q!")
        return redirect('home')
    
    professions = Profession.objects.annotate(
        student_count=Count('students', filter=models.Q(students__role='student')),
        teacher_count=Count('students', filter=models.Q(students__role='teacher'))
    )
    return render(request, 'accounts/admin/professions.html', {'professions': professions})


@login_required
def admin_profession_add(request):
    if not request.user.is_admin:
        messages.error(request, "Sizda admin paneliga kirish huquqi yo'q!")
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
        messages.error(request, "Sizda admin paneliga kirish huquqi yo'q!")
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
        messages.error(request, "Sizda admin paneliga kirish huquqi yo'q!")
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
        messages.error(request, "Sizda admin paneliga kirish huquqi yo'q!")
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
    
    context = {
        'users': users,
        'professions': professions,
        'role_filter': role_filter,
        'profession_filter': profession_filter,
        'status_filter': status_filter,
    }
    return render(request, 'accounts/admin/users.html', context)


@login_required
def admin_user_view(request, pk):
    if not request.user.is_admin:
        messages.error(request, "Sizda admin paneliga kirish huquqi yo'q!")
        return redirect('home')
    
    user = get_object_or_404(CustomUser, pk=pk)
    return render(request, 'accounts/admin/user_view.html', {'user_obj': user})


@login_required
def admin_user_edit(request, pk):
    if not request.user.is_admin:
        messages.error(request, "Sizda admin paneliga kirish huquqi yo'q!")
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
        messages.error(request, "Sizda admin paneliga kirish huquqi yo'q!")
        return redirect('home')
    
    user = get_object_or_404(CustomUser, pk=pk)
    
    if user.is_admin:
        messages.error(request, "Admin foydalanuvchini bloklash mumkin emas!")
        return redirect('admin_users')
    
    user.is_blocked = not user.is_blocked
    user.save()
    
    if user.is_blocked:
        messages.success(request, f"{user.full_name} bloklandi!")
    else:
        messages.success(request, f"{user.full_name} blokdan chiqarildi!")
    
    return redirect('admin_users')


@login_required
def admin_user_delete(request, pk):
    if not request.user.is_admin:
        messages.error(request, "Sizda admin paneliga kirish huquqi yo'q!")
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
        messages.error(request, "Sizda admin paneliga kirish huquqi yo'q!")
        return redirect('home')
    
    # Umumiy statistika
    total_users = CustomUser.objects.count()
    total_teachers = CustomUser.objects.filter(role='teacher').count()
    total_students = CustomUser.objects.filter(role='student').count()
    total_admins = CustomUser.objects.filter(role='admin').count()
    blocked_users = CustomUser.objects.filter(is_blocked=True).count()
    online_users = sum(1 for u in CustomUser.objects.all() if u.is_online)
    
    # Yo'nalishlar bo'yicha
    profession_stats = Profession.objects.annotate(
        student_count=Count('students', filter=models.Q(students__role='student')),
        teacher_count=Count('students', filter=models.Q(students__role='teacher')),
        total=Count('students')
    )
    
    # Kunlik ro'yxatdan o'tish (oxirgi 7 kun)
    from datetime import timedelta
    today = timezone.now().date()
    daily_registrations = []
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        count = CustomUser.objects.filter(date_joined__date=day).count()
        daily_registrations.append({'date': day, 'count': count})
    
    context = {
        'total_users': total_users,
        'total_teachers': total_teachers,
        'total_students': total_students,
        'total_admins': total_admins,
        'blocked_users': blocked_users,
        'online_users': online_users,
        'profession_stats': profession_stats,
        'daily_registrations': daily_registrations,
    }
    
    return render(request, 'accounts/admin/statistics.html', context)


@login_required
def admin_export_pdf(request):
    if not request.user.is_admin:
        messages.error(request, "Sizda admin paneliga kirish huquqi yo'q!")
        return redirect('home')
    
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from io import BytesIO
    
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
    elements = []
    styles = getSampleStyleSheet()
    
    # Title
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=20,
        spaceAfter=30,
        alignment=1
    )
    elements.append(Paragraph("LMS Statistika Hisoboti", title_style))
    elements.append(Paragraph(f"Sana: {timezone.now().strftime('%d.%m.%Y %H:%M')}", styles['Normal']))
    elements.append(Spacer(1, 20))
    
    # Umumiy statistika
    elements.append(Paragraph("Umumiy Statistika", styles['Heading2']))
    elements.append(Spacer(1, 10))
    
    total_users = CustomUser.objects.count()
    total_teachers = CustomUser.objects.filter(role='teacher').count()
    total_students = CustomUser.objects.filter(role='student').count()
    total_admins = CustomUser.objects.filter(role='admin').count()
    blocked_users = CustomUser.objects.filter(is_blocked=True).count()
    
    stats_data = [
        ['Ko\'rsatkich', 'Soni'],
        ['Jami foydalanuvchilar', str(total_users)],
        ['O\'quvchilar', str(total_students)],
        ['O\'qituvchilar', str(total_teachers)],
        ['Adminlar', str(total_admins)],
        ['Bloklangan', str(blocked_users)],
    ]
    
    stats_table = Table(stats_data, colWidths=[3*inch, 2*inch])
    stats_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0d6efd')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTSIZE', (0, 0), (-1, 0), 12),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#f8f9fa')),
        ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#dee2e6')),
        ('FONTSIZE', (0, 1), (-1, -1), 10),
        ('PADDING', (0, 0), (-1, -1), 8),
    ]))
    elements.append(stats_table)
    elements.append(Spacer(1, 30))
    
    # Yo'nalishlar statistikasi
    elements.append(Paragraph("Yo'nalishlar bo'yicha", styles['Heading2']))
    elements.append(Spacer(1, 10))
    
    profession_data = [['Yo\'nalish', 'O\'quvchilar', 'O\'qituvchilar', 'Jami']]
    
    professions = Profession.objects.annotate(
        student_count=Count('students', filter=models.Q(students__role='student')),
        teacher_count=Count('students', filter=models.Q(students__role='teacher')),
        total=Count('students')
    )
    
    for p in professions:
        profession_data.append([p.name, str(p.student_count), str(p.teacher_count), str(p.total)])
    
    if len(profession_data) > 1:
        prof_table = Table(profession_data, colWidths=[2.5*inch, 1.2*inch, 1.2*inch, 1*inch])
        prof_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#198754')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTSIZE', (0, 0), (-1, 0), 11),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#f8f9fa')),
            ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#dee2e6')),
            ('FONTSIZE', (0, 1), (-1, -1), 10),
            ('PADDING', (0, 0), (-1, -1), 8),
        ]))
        elements.append(prof_table)
    
    elements.append(Spacer(1, 30))
    
    # Foydalanuvchilar ro'yxati
    elements.append(Paragraph("Foydalanuvchilar ro'yxati", styles['Heading2']))
    elements.append(Spacer(1, 10))
    
    users_data = [['#', 'Ism Familiya', 'Username', 'Rol', 'Yo\'nalish', 'Status']]
    
    for i, user in enumerate(CustomUser.objects.all()[:50], 1):
        role = 'Admin' if user.role == 'admin' else ('O\'qituvchi' if user.role == 'teacher' else 'O\'quvchi')
        status = 'Bloklangan' if user.is_blocked else 'Faol'
        profession_name = user.profession.name if user.profession else '-'
        users_data.append([str(i), user.full_name, user.username, role, profession_name, status])
    
    users_table = Table(users_data, colWidths=[0.4*inch, 1.5*inch, 1.2*inch, 1*inch, 1.3*inch, 0.8*inch])
    users_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#6f42c1')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 10),
        ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#f8f9fa')),
        ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#dee2e6')),
        ('FONTSIZE', (0, 1), (-1, -1), 8),
        ('PADDING', (0, 0), (-1, -1), 6),
    ]))
    elements.append(users_table)
    
    doc.build(elements)
    
    buffer.seek(0)
    response = HttpResponse(buffer, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="LMS_Statistika_{timezone.now().strftime("%Y%m%d_%H%M")}.pdf"'
    
    return response
