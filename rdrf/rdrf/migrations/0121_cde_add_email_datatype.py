# Generated by Django 2.2.10 on 2020-02-21 15:38

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rdrf', '0120_duration_cde_datatype'),
    ]

    operations = [
        migrations.AlterField(
            model_name='commondataelement',
            name='datatype',
            field=models.CharField(choices=[('boolean', 'Boolean'), ('calculated', 'Calculated'), ('date', 'Date'), ('duration', 'Duration'), ('email', 'Email'), ('file', 'File'), ('float', 'Float'), ('integer', 'Integer'), ('range', 'Range'), ('string', 'String'), ('time', 'Time')], default='string', help_text='type of field', max_length=50),
        ),
    ]
