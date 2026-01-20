from django import forms
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from .models import (
    CustomUser, Profession, Section, Lesson, VideoLesson, Homework, 
    Test, TestQuestion, TestAnswer, HomeworkSubmission, Certificate
)


class RegisterForm(UserCreationForm):
    first_name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Ismingiz'})
    )
    last_name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Familiyangiz'})
    )
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Username'})
    )
    phone = forms.CharField(
        max_length=20,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': '+998 90 123 45 67'})
    )
    role = forms.ChoiceField(
        choices=[('student', 'O\'quvchi'), ('teacher', 'O\'qituvchi')],
        widget=forms.Select(attrs={'class': 'form-control'})
    )
    password1 = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': 'Parol'})
    )
    password2 = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': 'Parolni tasdiqlang'})
    )

    class Meta:
        model = CustomUser
        fields = ['first_name', 'last_name', 'username', 'phone', 'role', 'password1', 'password2']


class LoginForm(forms.Form):
    username = forms.CharField(
        widget=forms.TextInput(attrs={
            "class": "form-control ps-5",
            "placeholder": "Username"
        })
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={
            "class": "form-control ps-5 pe-5",
            "placeholder": "Parol"
        })
    )




class ProfessionForm(forms.ModelForm):
    name = forms.CharField(
        max_length=200,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Kasb nomini kiriting'})
    )
    photo = forms.ImageField(widget=forms.FileInput(attrs={'class': 'form-control'}))
    description = forms.CharField(
        widget=forms.Textarea(attrs={'class': 'form-control', 'placeholder': 'Kasb haqida ma\'lumot', 'rows': 4})
    )

    class Meta:
        model = Profession
        fields = ['name', 'photo', 'description']


class UserProfileForm(forms.ModelForm):
    first_name = forms.CharField(max_length=100, widget=forms.TextInput(attrs={'class': 'form-control'}))
    last_name = forms.CharField(max_length=100, widget=forms.TextInput(attrs={'class': 'form-control'}))
    phone = forms.CharField(max_length=20, widget=forms.TextInput(attrs={'class': 'form-control'}))
    photo = forms.ImageField(required=False, widget=forms.FileInput(attrs={'class': 'form-control'}))
    bio = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'O\'zingiz haqida...'})
    )

    address = forms.CharField(required=False, widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Yashash manzilingiz'}))
    birth_date = forms.DateField(required=False, widget=forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}))
    id_card = forms.FileField(required=False, widget=forms.FileInput(attrs={'class': 'form-control'}))
    birth_certificate = forms.FileField(required=False, widget=forms.FileInput(attrs={'class': 'form-control'}))

    class Meta:
        model = CustomUser
        fields = ['first_name', 'last_name', 'email', 'phone', 'address', 'birth_date', 'id_card', 'birth_certificate', 'photo', 'bio']


class AdminUserEditForm(forms.ModelForm):
    first_name = forms.CharField(max_length=100, widget=forms.TextInput(attrs={'class': 'form-control'}))
    last_name = forms.CharField(max_length=100, widget=forms.TextInput(attrs={'class': 'form-control'}))
    username = forms.CharField(max_length=150, widget=forms.TextInput(attrs={'class': 'form-control'}))
    phone = forms.CharField(max_length=20, widget=forms.TextInput(attrs={'class': 'form-control'}))
    role = forms.ChoiceField(choices=CustomUser.ROLE_CHOICES, widget=forms.Select(attrs={'class': 'form-control'}))
    profession = forms.ModelChoiceField(
        queryset=Profession.objects.all(), required=False,
        widget=forms.Select(attrs={'class': 'form-control'}), empty_label="Yo'nalishni tanlang"
    )
    photo = forms.ImageField(required=False, widget=forms.FileInput(attrs={'class': 'form-control'}))
    bio = forms.CharField(required=False, widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3}))
    is_blocked = forms.BooleanField(required=False, widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}))
    new_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': 'Yangi parol'})
    )

    address = forms.CharField(required=False, widget=forms.TextInput(attrs={'class': 'form-control'}))
    birth_date = forms.DateField(required=False, widget=forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}))
    id_card = forms.FileField(required=False, widget=forms.FileInput(attrs={'class': 'form-control'}))
    birth_certificate = forms.FileField(required=False, widget=forms.FileInput(attrs={'class': 'form-control'}))

    class Meta:
        model = CustomUser
        fields = ['first_name', 'last_name', 'username', 'phone', 'role', 'profession', 'photo', 'bio', 'is_blocked', 'address', 'birth_date', 'id_card', 'birth_certificate']


class ChangePasswordForm(forms.Form):
    old_password = forms.CharField(widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': 'Joriy parol'}))
    new_password1 = forms.CharField(widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': 'Yangi parol'}))
    new_password2 = forms.CharField(widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': 'Yangi parolni tasdiqlang'}))

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get('new_password1') != cleaned_data.get('new_password2'):
            raise forms.ValidationError("Parollar mos kelmaydi!")
        return cleaned_data


class VideoLessonForm(forms.Form):
    title = forms.CharField(
        max_length=300,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Dars nomi'})
    )
    video_url = forms.URLField(
        required=True,
        widget=forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'YouTube yoki Vimeo havolasi'})
    )
    duration = forms.IntegerField(
        min_value=1,
        initial=10,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'Davomiyligi (daqiqa)'}),
        help_text="Video davomiyligi (daqiqada). O'quvchi shu vaqtning yarmini ko'rishi kerak."
    )


class HomeworkForm(forms.Form):
    title = forms.CharField(
        max_length=300,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Vazifa nomi'})
    )
    description = forms.CharField(
        widget=forms.Textarea(attrs={'class': 'form-control', 'placeholder': 'Vazifa tavsifi', 'rows': 4})
    )


class HomeworkSubmissionForm(forms.ModelForm):
    file = forms.FileField(widget=forms.FileInput(attrs={'class': 'form-control'}))

    class Meta:
        model = HomeworkSubmission
        fields = ['file']


class HomeworkGradeForm(forms.Form):
    grade = forms.IntegerField(
        min_value=1, max_value=100,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'Baho (1-100)'})
    )
    feedback = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-control', 'placeholder': 'Izoh', 'rows': 3})
    )


class TestForm(forms.Form):
    title = forms.CharField(
        max_length=300,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Test nomi'})
    )
    time_limit = forms.IntegerField(
        initial=30,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'Vaqt (daqiqa)'})
    )
    passing_score = forms.IntegerField(
        initial=60,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'O\'tish bali (%)'})
    )


class TestQuestionForm(forms.ModelForm):
    question_text = forms.CharField(
        widget=forms.Textarea(attrs={'class': 'form-control', 'placeholder': 'Savol matni', 'rows': 3})
    )
    question_image = forms.ImageField(
        required=False,
        widget=forms.FileInput(attrs={'class': 'form-control'})
    )
    answer1 = forms.CharField(
        max_length=500,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': '1-javob'})
    )
    answer2 = forms.CharField(
        max_length=500,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': '2-javob'})
    )
    answer3 = forms.CharField(
        max_length=500,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': '3-javob'})
    )
    correct_answer = forms.ChoiceField(
        choices=[(1, '1-javob'), (2, '2-javob'), (3, '3-javob')],
        widget=forms.RadioSelect(attrs={'class': 'form-check-input'})
    )

    class Meta:
        model = TestQuestion
        fields = ['question_text', 'question_image']


class CertificateForm(forms.ModelForm):
    file = forms.FileField(widget=forms.FileInput(attrs={'class': 'form-control'}))

    class Meta:
        model = Certificate
        fields = ['file']


class SectionForm(forms.ModelForm):
    title = forms.CharField(
        max_length=200,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': "Bo'lim nomi"})
    )
    description = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-control', 'placeholder': 'Tavsif (ixtiyoriy)', 'rows': 3})
    )
    order = forms.IntegerField(
        initial=0,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'Tartib raqami'})
    )

    class Meta:
        model = Section
        fields = ['title', 'description', 'order']
