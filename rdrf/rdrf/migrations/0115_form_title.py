# Generated by Django 2.1.12 on 2019-11-15 13:06

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('auth', '0009_alter_user_last_name_max_length'),
        ('rdrf', '0114_registry_forms_add_default_ordering'),
    ]

    operations = [
        migrations.CreateModel(
            name='FormTitle',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('default_title', models.CharField(choices=[('Demographics', 'Demographics'), ('Consents', 'Consents'), ('Clinician', 'Clinician'), ('Proms', 'Proms'), ('Family linkage', 'Family Linkage')], max_length=50)),
                ('custom_title', models.CharField(max_length=50)),
                ('order', models.PositiveIntegerField()),
                ('groups', models.ManyToManyField(help_text='Users of these groups will see the custom title instead of the default one', to='auth.Group')),
                ('registry', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='rdrf.Registry')),
            ],
            options={
                'ordering': ('registry', 'default_title', 'order'),
            },
        ),
    ]
