from django.db import models
from django.conf import settings


class Post(models.Model):
    POST_TYPES = (
        ('news', 'Yangiliklar'),
        ('tutorial', 'Darslik'),
        ('tips', 'Maslahatlar'),
        ('announcement', 'E\'lon'),
        ('other', 'Boshqa'),
    )
    
    title = models.CharField(max_length=300, verbose_name="Sarlavha")
    content = models.TextField(verbose_name="Matn")
    image = models.ImageField(upload_to='posts/', blank=True, null=True, verbose_name="Rasm")
    post_type = models.CharField(max_length=20, choices=POST_TYPES, default='news', verbose_name="Post turi")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='posts')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_published = models.BooleanField(default=True, verbose_name="Chop etilgan")
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = "Post"
        verbose_name_plural = "Postlar"
    
    def __str__(self):
        return self.title
    
    @property
    def likes_count(self):
        return self.likes.count()
    
    @property
    def comments_count(self):
        return self.comments.count()


class PostLike(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='likes')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='post_likes')
    created_at = models.DateTimeField(auto_now_add=True)
    coin_awarded = models.BooleanField(default=False)
    
    class Meta:
        unique_together = ['post', 'user']
        verbose_name = "Like"
        verbose_name_plural = "Likelar"
    
    def __str__(self):
        return f"{self.user.full_name} - {self.post.title}"


class PostComment(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='comments')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='post_comments')
    content = models.TextField(verbose_name="Izoh")
    created_at = models.DateTimeField(auto_now_add=True)
    coin_awarded = models.BooleanField(default=False)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = "Izoh"
        verbose_name_plural = "Izohlar"
    
    def __str__(self):
        return f"{self.user.full_name}: {self.content[:30]}"
    
    @property
    def likes_count(self):
        return self.likes.count()


class CommentLike(models.Model):
    comment = models.ForeignKey(PostComment, on_delete=models.CASCADE, related_name='likes')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='comment_likes')
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ['comment', 'user']
        verbose_name = "Izoh Like"
        verbose_name_plural = "Izoh Likelar"
    
    def __str__(self):
        return f"{self.user.full_name} liked {self.comment}"
