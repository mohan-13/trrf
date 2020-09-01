from db.transformer.transformer import clinical_data_generator, CompositeKey
from rdrf.models.definition.models import ClinicalData


def move_cde(registry_code, source: CompositeKey, destination: CompositeKey):
    moved_data = []

    for entry, data in clinical_data_generator(registry_code):
        for form in data["forms"]:
            if form["name"] != source.form:
                continue
            for section in form["sections"]:
                if section["name"] != source.section:
                    continue
                for cde in section["cdes"][:]:
                    if cde["name"] != source.cde:
                        continue

                    moved_data.append(cde)
                    section["cdes"].remove(cde)

    for entry, data in clinical_data_generator(registry_code):
        for form in data["forms"]:
            if form["name"] != source.form:
                continue
            for section in form["sections"]:
                if section["name"] != source.section:
                    continue
                for cde in section["cdes"][:]:
                    if cde["name"] != source.cde:
                        continue


def has_section(entry, source):
    pass


def move_section(registry_code, source, destination):
    entries = ClinicalData.objects.collection(registry_code, "cdes")
    for entry in entries:
        if has_section(entry, source):
            pass

    pass
