from rdrf.models.definition.models import ClinicalData


def move_cde(registry_code, source_code, destination_code):
    pass


def has_section(entry, source):
    pass


def move_section(registry_code, source, destination):
    entries = ClinicalData.objects.collection(registry_code, "cdes")
    for entry in entries:
        if has_section(entry, source):
            pass

    pass
