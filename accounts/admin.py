from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import CustomUser


class CustomUserAdmin(UserAdmin):
    model = CustomUser
    list_display = ['username', 'first_name', 'last_name', 'phone', 'is_staff']
    fieldsets = UserAdmin.fieldsets + (
        ('Qo\'shimcha ma\'lumotlar', {'fields': ('phone',)}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ('Qo\'shimcha ma\'lumotlar', {'fields': ('first_name', 'last_name', 'phone',)}),
    )


admin.site.register(CustomUser, CustomUserAdmin)
