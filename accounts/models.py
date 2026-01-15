from django.db import models
from django.contrib.auth.models import AbstractUser


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
    
    class Meta:
        verbose_name = "Foydalanuvchi"
        verbose_name_plural = "Foydalanuvchilar"


class Profession(models.Model):
    name = models.CharField(max_length=200, verbose_name="Kasb nomi")
    photo = models.ImageField(upload_to='professions/', verbose_name="Rasm")
    description = models.TextField(verbose_name="Kasb haqida")
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return self.name
    
    class Meta:
        verbose_name = "Kasb"
        verbose_name_plural = "Kasblar"
        ordering = ['-created_at']
