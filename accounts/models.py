from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils import timezone


class Profession(models.Model):
    name = models.CharField(max_length=200, verbose_name="Kasb nomi")
    photo = models.ImageField(upload_to='professions/', verbose_name="Rasm")
    description = models.TextField(verbose_name="Kasb haqida")
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return self.name
    
    def student_count(self):
        return self.students.filter(role='student').count()
    
    def teacher_count(self):
        return self.students.filter(role='teacher').count()
    
    class Meta:
        verbose_name = "Kasb"
        verbose_name_plural = "Kasblar"
        ordering = ['-created_at']


class CustomUser(AbstractUser):
    ROLE_CHOICES = (
        ('admin', 'Admin'),
        ('teacher', 'O\'qituvchi'),
        ('student', 'O\'quvchi'),
    )
    
    first_name = models.CharField(max_length=100, verbose_name="Ism")
    last_name = models.CharField(max_length=100, verbose_name="Familiya")
    phone = models.CharField(max_length=20, unique=True, verbose_name="Telefon raqam")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='student', verbose_name="Rol")
    photo = models.ImageField(upload_to='profiles/', blank=True, null=True, verbose_name="Profil rasmi")
    profession = models.ForeignKey(Profession, on_delete=models.SET_NULL, null=True, blank=True, related_name='students', verbose_name="Yo'nalish")
    is_blocked = models.BooleanField(default=False, verbose_name="Bloklangan")
    last_activity = models.DateTimeField(null=True, blank=True, verbose_name="Oxirgi faollik")
    bio = models.TextField(blank=True, null=True, verbose_name="O'zi haqida")
    
    def __str__(self):
        return self.username
    
    @property
    def is_admin(self):
        return self.role == 'admin'
    
    @property
    def is_teacher(self):
        return self.role == 'teacher'
    
    @property
    def is_student(self):
        return self.role == 'student'
    
    @property
    def is_online(self):
        if self.last_activity:
            now = timezone.now()
            diff = now - self.last_activity
            return diff.total_seconds() < 300  # 5 daqiqa ichida faol
        return False
    
    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"
    
    def get_initials(self):
        first = self.first_name[0] if self.first_name else ''
        last = self.last_name[0] if self.last_name else ''
        return f"{first}{last}".upper()
    
    class Meta:
        verbose_name = "Foydalanuvchi"
        verbose_name_plural = "Foydalanuvchilar"
