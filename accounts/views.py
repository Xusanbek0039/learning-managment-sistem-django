from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from .forms import RegisterForm, LoginForm, ProfessionForm
from .models import Profession


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
                login(request, user)
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


# Admin Panel Views
@login_required
def admin_dashboard(request):
    if not request.user.is_admin:
        messages.error(request, "Sizda admin paneliga kirish huquqi yo'q!")
        return redirect('home')
    
    from .models import CustomUser
    context = {
        'total_users': CustomUser.objects.count(),
        'total_teachers': CustomUser.objects.filter(role='teacher').count(),
        'total_students': CustomUser.objects.filter(role='student').count(),
        'total_professions': Profession.objects.count(),
    }
    return render(request, 'accounts/admin/dashboard.html', context)


@login_required
def admin_professions(request):
    if not request.user.is_admin:
        messages.error(request, "Sizda admin paneliga kirish huquqi yo'q!")
        return redirect('home')
    
    professions = Profession.objects.all()
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
    
    from .models import CustomUser
    users = CustomUser.objects.all()
    return render(request, 'accounts/admin/users.html', {'users': users})
