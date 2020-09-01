from copy import deepcopy
from enum import Enum

from models.definition.models import ClinicalData
from .move import move_cde, move_section
from .convert import convert_cde
from .edit import edit_cde


# TODO: Update progress after change?


class Operations(Enum):
    # Move CDE from one Form Section to another
    CDE_MOVE = "CDE_MOVE"

    # Move Section from one Form to another
    SECTION_MOVE = "SECTION_MOVE"

    # Convert CDE from one datatype to another
    CDE_CONVERT = "CDE_CONVERT"

    # Edit CDE model fields
    CDE_EDIT = "CDE_EDIT"

    # Edit Section model fields
    SECTION_EDIT = "SECTION_EDIT"

    # Edit Form model fields
    FORM_EDIT = "FORM_EDIT"


class CompositeKey:
    form = None
    section = None
    cde = None

    def __init__(self, form=None, section=None, cde=None):
        self.form = form
        self.section = section
        self.cde = cde


def transform(operation, *args):
    """
    Run a clinical database transformation operation

    :param operation: A clinical database operation
    :type operation: Operations
    :param args: The operation's arguments
    :type args: List[str]
    """

    if operation == Operations.CDE_MOVE:
        move_cde(*args)
    elif operation == Operations.SECTION_MOVE:
        move_section(*args)
    elif operation == Operations.CDE_CONVERT:
        convert_cde(*args)
    elif operation == Operations.CDE_EDIT:
        edit_cde(*args)
    else:
        raise NotImplementedError("Operation not available")


def clinical_data_generator(registry):
    for entry in ClinicalData.objects.filter(registry_code=registry, collection__in=["cdes", "history"]):
        yield entry, deepcopy(entry.data)
