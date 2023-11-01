from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

# Register your models here.

from database.models import (
    KhojUser,
    ChatModelOptions,
    OpenAIProcessorConversationConfig,
    OfflineChatProcessorConversationConfig,
)

admin.site.register(KhojUser, UserAdmin)

admin.site.register(ChatModelOptions)
admin.site.register(OpenAIProcessorConversationConfig)
admin.site.register(OfflineChatProcessorConversationConfig)
