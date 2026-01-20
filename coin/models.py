from django.db import models
from django.conf import settings


class Product(models.Model):
    name = models.CharField(max_length=200, verbose_name="Mahsulot nomi")
    description = models.TextField(verbose_name="Mahsulot haqida")
    image = models.ImageField(upload_to='products/', verbose_name="Rasm")
    coin_price = models.IntegerField(verbose_name="Narxi (Coin)")
    stock = models.IntegerField(default=0, verbose_name="Omborda")
    is_active = models.BooleanField(default=True, verbose_name="Faol")
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = "Mahsulot"
        verbose_name_plural = "Mahsulotlar"
    
    def __str__(self):
        return self.name
    
    @property
    def likes_count(self):
        return self.likes.count()
    
    @property
    def comments_count(self):
        return self.comments.count()


class ProductLike(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='likes')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='product_likes')
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ['product', 'user']
        verbose_name = "Mahsulot like"
        verbose_name_plural = "Mahsulot likelari"


class ProductComment(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='comments')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='product_comments')
    content = models.TextField(verbose_name="Izoh")
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = "Mahsulot izohi"
        verbose_name_plural = "Mahsulot izohlari"


class ProductPurchase(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='purchases')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='purchases')
    coins_spent = models.IntegerField(verbose_name="Sarflangan coin")
    purchased_at = models.DateTimeField(auto_now_add=True)
    is_delivered = models.BooleanField(default=False, verbose_name="Yetkazildi")
    
    class Meta:
        ordering = ['-purchased_at']
        verbose_name = "Xarid"
        verbose_name_plural = "Xaridlar"


class CoinTransaction(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='coin_transactions')
    amount = models.IntegerField()
    reason = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.user.full_name}: +{self.amount} coin"


class ActivityLog(models.Model):
    ACTION_TYPES = (
        ('login', 'Tizimga kirdi'),
        ('logout', 'Tizimdan chiqdi'),
        ('like_post', 'Postga like bosdi'),
        ('comment_post', 'Postga izoh qoldirdi'),
        ('like_product', 'Mahsulotga like bosdi'),
        ('comment_product', 'Mahsulotga izoh qoldirdi'),
        ('submit_homework', 'Vazifa topshirdi'),
        ('homework_graded', 'Vazifasi baholandi'),
        ('submit_test', 'Test topshirdi'),
        ('watch_video', 'Video ko\'rdi'),
        ('enroll_course', 'Kursga yozildi'),
        ('purchase_product', 'Mahsulot sotib oldi'),
        ('earn_coins', 'Coin oldi'),
    )
    
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='activities')
    action_type = models.CharField(max_length=30, choices=ACTION_TYPES)
    description = models.TextField(verbose_name="Tavsif")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = "Faollik"
        verbose_name_plural = "Faolliklar"
    
    def __str__(self):
        return f"{self.user.full_name} - {self.get_action_type_display()}"
