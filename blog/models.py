from django.db import models
from accounts.models import CustomUser

class Post(models.Model):
    POST_TYPES = (
        ('news', 'Yangilik'),
        ('article', 'Maqola'),
        ('announcement', 'E\'lon'),
    )
    
    title = models.CharField(max_length=200, verbose_name="Sarlavha")
    content = models.TextField(verbose_name="Matn")
    image = models.ImageField(upload_to='blog_images/', blank=True, null=True, verbose_name="Rasm")
    author = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='posts', verbose_name="Muallif")
    post_type = models.CharField(max_length=20, choices=POST_TYPES, default='article', verbose_name="Maqola turi")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Yaratilgan vaqt")
    
    class Meta:
        verbose_name = "Maqola"
        verbose_name_plural = "Maqolalar"
        ordering = ['-created_at']

    def __str__(self):
        return self.title

class Comment(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='comments')
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='comments')
    text = models.TextField(verbose_name="Izoh")
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        verbose_name = "Izoh"
        verbose_name_plural = "Izohlar"
        ordering = ['created_at']
        
    def __str__(self):
        return f"{self.user.username} - {self.post.title}"

class Like(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='likes')
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='likes')
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ['post', 'user']
        verbose_name = "Like"
        verbose_name_plural = "Likelar"
