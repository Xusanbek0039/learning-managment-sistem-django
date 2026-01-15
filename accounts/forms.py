from django import forms
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from .models import CustomUser, Profession


class RegisterForm(UserCreationForm):
    first_name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Ismingiz'
        })
    )
    last_name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Familiyangiz'
        })
    )
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Username'
        })
    )
    phone = forms.CharField(
        max_length=20,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': '+998 90 123 45 67'
        })
    )
    role = forms.ChoiceField(
        choices=[('student', 'O\'quvchi'), ('teacher', 'O\'qituvchi')],
        widget=forms.Select(attrs={
            'class': 'form-control'
        })
    )
    profession = forms.ModelChoiceField(
        queryset=Profession.objects.all(),
        required=False,
        widget=forms.Select(attrs={
            'class': 'form-control'
        }),
        empty_label="Yo'nalishni tanlang"
    )
    password1 = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Parol'
        })
    )
    password2 = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Parolni tasdiqlang'
        })
    )

    class Meta:
        model = CustomUser
        fields = ['first_name', 'last_name', 'username', 'phone', 'role', 'profession', 'password1', 'password2']


class LoginForm(AuthenticationForm):
    username = forms.CharField(
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Username'
        })
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Parol'
        })
    )


class ProfessionForm(forms.ModelForm):
    name = forms.CharField(
        max_length=200,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Kasb nomini kiriting'
        })
    )
    photo = forms.ImageField(
        widget=forms.FileInput(attrs={
            'class': 'form-control'
        })
    )
    description = forms.CharField(
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'placeholder': 'Kasb haqida ma\'lumot kiriting',
            'rows': 4
        })
    )

    class Meta:
        model = Profession
        fields = ['name', 'photo', 'description']


class UserProfileForm(forms.ModelForm):
    first_name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={'class': 'form-control'})
    )
    last_name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={'class': 'form-control'})
    )
    phone = forms.CharField(
        max_length=20,
        widget=forms.TextInput(attrs={'class': 'form-control'})
    )
    photo = forms.ImageField(
        required=False,
        widget=forms.FileInput(attrs={'class': 'form-control'})
    )
    profession = forms.ModelChoiceField(
        queryset=Profession.objects.all(),
        required=False,
        widget=forms.Select(attrs={'class': 'form-control'}),
        empty_label="Yo'nalishni tanlang"
    )
    bio = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 3,
            'placeholder': 'O\'zingiz haqida...'
        })
    )

    class Meta:
        model = CustomUser
        fields = ['first_name', 'last_name', 'phone', 'photo', 'profession', 'bio']


class AdminUserEditForm(forms.ModelForm):
    first_name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={'class': 'form-control'})
    )
    last_name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={'class': 'form-control'})
    )
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={'class': 'form-control'})
    )
    phone = forms.CharField(
        max_length=20,
        widget=forms.TextInput(attrs={'class': 'form-control'})
    )
    role = forms.ChoiceField(
        choices=CustomUser.ROLE_CHOICES,
        widget=forms.Select(attrs={'class': 'form-control'})
    )
    profession = forms.ModelChoiceField(
        queryset=Profession.objects.all(),
        required=False,
        widget=forms.Select(attrs={'class': 'form-control'}),
        empty_label="Yo'nalishni tanlang"
    )
    photo = forms.ImageField(
        required=False,
        widget=forms.FileInput(attrs={'class': 'form-control'})
    )
    bio = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3})
    )
    is_blocked = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'})
    )
    new_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Yangi parol (o\'zgartirish uchun)'
        })
    )

    class Meta:
        model = CustomUser
        fields = ['first_name', 'last_name', 'username', 'phone', 'role', 'profession', 'photo', 'bio', 'is_blocked']


class ChangePasswordForm(forms.Form):
    old_password = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Joriy parol'
        })
    )
    new_password1 = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Yangi parol'
        })
    )
    new_password2 = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Yangi parolni tasdiqlang'
        })
    )

    def clean(self):
        cleaned_data = super().clean()
        new_password1 = cleaned_data.get('new_password1')
        new_password2 = cleaned_data.get('new_password2')
        
        if new_password1 and new_password2 and new_password1 != new_password2:
            raise forms.ValidationError("Parollar mos kelmaydi!")
        
        return cleaned_data
