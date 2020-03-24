# Generated by Django 2.2.10 on 2020-03-24 15:40

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rdrf', '0122_new_carer_notification'),
    ]

    operations = [
        migrations.AlterField(
            model_name='emailnotification',
            name='email_from',
            field=models.EmailField(blank=True, help_text='Leave empty for default email address', max_length=254, null=True),
        ),
    ]
