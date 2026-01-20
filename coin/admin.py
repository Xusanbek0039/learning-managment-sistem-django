from django.contrib import admin
from .models import Product, ProductLike, ProductComment, ProductPurchase, CoinTransaction, ActivityLog


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ['name', 'coin_price', 'stock', 'is_active', 'created_at']
    list_filter = ['is_active', 'created_at']
    search_fields = ['name', 'description']
    list_editable = ['is_active', 'stock']


@admin.register(ProductLike)
class ProductLikeAdmin(admin.ModelAdmin):
    list_display = ['product', 'user', 'created_at']
    list_filter = ['created_at']


@admin.register(ProductComment)
class ProductCommentAdmin(admin.ModelAdmin):
    list_display = ['product', 'user', 'created_at']
    list_filter = ['created_at']
    search_fields = ['content']


@admin.register(ProductPurchase)
class ProductPurchaseAdmin(admin.ModelAdmin):
    list_display = ['product', 'user', 'coins_spent', 'is_delivered', 'purchased_at']
    list_filter = ['is_delivered', 'purchased_at']
    list_editable = ['is_delivered']


@admin.register(CoinTransaction)
class CoinTransactionAdmin(admin.ModelAdmin):
    list_display = ['user', 'amount', 'reason', 'created_at']
    list_filter = ['created_at']
    search_fields = ['user__username', 'reason']


@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    list_display = ['user', 'action_type', 'description', 'created_at']
    list_filter = ['action_type', 'created_at']
    search_fields = ['user__username', 'description']
