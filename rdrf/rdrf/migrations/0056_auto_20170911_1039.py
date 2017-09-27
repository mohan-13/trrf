# -*- coding: utf-8 -*-
# Generated by Django 1.10.7 on 2017-09-11 10:39
from __future__ import unicode_literals
from django.db import migrations
from django.db.utils import ProgrammingError

class ClinicalDBRunPython(migrations.RunPython):
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        # RunPython has access to all models. Ensure that all models are
        # reloaded in case any are delayed.
        from_state.clear_delayed_apps_cache()
        #if router.allow_migrate(schema_editor.connection.alias, app_label, **self.hints):
        if schema_editor.connection.alias == 'clinical':
            # We now execute the Python code in a context that contains a 'models'
            # object, representing the versioned models as an app registry.
            # We could try to override the global cache, but then people will still
            # use direct imports, so we go with a documentation approach instead.
            self.code(from_state.apps, schema_editor)


def extract_model_field_data_from_clinical_data_json_field(apps, schema_editor):
    # Our database router ignores hints (below)
    # from the Django source code this looks like dbname
    print("Extracting ClinicalData field data ..")
    dbname = schema_editor.connection.alias
    print("This is database %s" % dbname)
    if dbname != "clinical":
        print("Not running on non-clinical database")
        return
    
    try:
        ClinicalData = apps.get_model("rdrf", "ClinicalData")
        num_records = ClinicalData.objects.all().count()
        print("Number of ClinicalData objects = %s" % num_records)
        print("Starting to iterate through ClinicalData objects")
        for clinical_data in ClinicalData.objects.all():
            clinical_data.django_model = clinical_data.data.get("django_model", "")
            clinical_data.django_id = clinical_data.data.get("django_id", 0)
            clinical_data.context_id = clinical_data.data.get("context_id", None)
            clinical_data.save()
            print("Saved clinical data %s OK" % clinical_data.pk)
        print("All done")
    except ProgrammingError:
        # thrown when run on wrong db
        print("not running clinical data update on main db")


class Migration(migrations.Migration):

    dependencies = [
        ('rdrf', '0055_auto_20170911_1034'),
    ]

    operations = [
        ClinicalDBRunPython(extract_model_field_data_from_clinical_data_json_field),
    ]
