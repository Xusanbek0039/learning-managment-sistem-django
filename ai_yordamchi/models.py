from django.db import models
from django.conf import settings


class ChatSession(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='chat_sessions')
    title = models.CharField(max_length=200, default="Yangi suhbat")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)
    
    class Meta:
        ordering = ['-updated_at']
        verbose_name = "Suhbat"
        verbose_name_plural = "Suhbatlar"
    
    def __str__(self):
        return f"{self.user.full_name} - {self.title}"


class ChatMessage(models.Model):
    ROLE_CHOICES = (
        ('user', 'Foydalanuvchi'),
        ('assistant', 'Creative AI'),
    )
    
    session = models.ForeignKey(ChatSession, on_delete=models.CASCADE, related_name='messages')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    content = models.TextField()
    image = models.ImageField(upload_to='chat_images/', blank=True, null=True)
    coins_spent = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['created_at']
        verbose_name = "Xabar"
        verbose_name_plural = "Xabarlar"
    
    def __str__(self):
        return f"{self.role}: {self.content[:50]}"
