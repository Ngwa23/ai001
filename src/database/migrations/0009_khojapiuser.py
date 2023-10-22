# Generated by Django 4.2.5 on 2023-10-22 02:14

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("database", "0008_alter_conversation_conversation_log"),
    ]

    operations = [
        migrations.CreateModel(
            name="KhojApiUser",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token", models.CharField(max_length=50)),
                ("name", models.CharField(max_length=50)),
                ("accessed_at", models.DateTimeField(default=None, null=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]