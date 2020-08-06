from datetime import datetime

from registry.patients.models import Patient
from rdrf.models.definition.models import ClinicalData, Registry
from rdrf.db.transformer.move import move_section
from rdrf.db.transformer.transformer import CompositeKey
from django.test import TestCase


class MoveTest(TestCase):
    databases = ['default', 'clinical']

    def setUp(self):
        super().setUp()

        self.registry = Registry.objects.create(
            name="Transformation Registry",
            code="tfr",
        )

        self.patient = Patient.objects.create(
            family_name="Patient",
            given_names="Transformation",
            date_of_birth=datetime.now(),
            consent=True
        )
        self.patient.rdrf_registry.add(self.registry)

        clinical_datum = ClinicalData.create(
            self.patient,
            registry_code=self.registry.code,
            context_id=None,
            collection="cdes",
            data={
                "hello": "world"
            })
        clinical_datum.save()

    def test_move_section(self):
        move_section(self.registry.code, CompositeKey("form1", "section1", "cde1"), CompositeKey("form2", "section2", "cde2"))
