from django.contrib import admin
from .models import ChatSession, ChatMessage


class ChatMessageInline(admin.TabularInline):
    model = ChatMessage
    extra = 0
    readonly_fields = ("role", "text", "file", "created_at")
    can_delete = False


@admin.register(ChatSession)
class ChatSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "created_at", "message_count")
    list_filter = ("created_at",)
    search_fields = ("user__username", "user__first_name", "user__last_name")
    date_hierarchy = "created_at"
    inlines = [ChatMessageInline]

    def message_count(self, obj):
        return obj.messages.count()
    message_count.short_description = "Xabarlar"


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("id", "chat", "role", "created_at")
    list_filter = ("role", "created_at")
    search_fields = ("text", "chat__user__username")
    readonly_fields = ("chat", "role", "text", "file", "created_at")
