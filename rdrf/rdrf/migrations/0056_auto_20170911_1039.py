# -*- coding: utf-8 -*-
# Generated by Django 1.10.7 on 2017-09-11 10:39
from __future__ import unicode_literals

from django.db import migrations


def extract_model_field_data_from_clinical_data_json_field(apps, schema_editor):
    ClinicalData = apps.get_model("rdrf", "ClinicalData")
    for clinical_data in ClinicalData.objects.all():
        clinical_data.django_model = clinical_data.data.get("django_model", "")
        clinical_data.django_id = clinical_data.data.get("django_id", 0)
        clinical_data.context_id = clinical_data.data.get("context_id", None)
        clinical_data.save()


class Migration(migrations.Migration):

    dependencies = [
        ('rdrf', '0055_auto_20170911_1034'),
    ]

    operations = [
        migrations.RunPython(extract_model_field_data_from_clinical_data_json_field),
    ]
