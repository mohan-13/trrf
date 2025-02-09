import datetime
import json
import jsonschema
import logging
import os.path
import yaml

from django.conf import settings
from django.contrib.auth.models import Group
from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.urls import reverse
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey
from django.db import models
from django.db.models.signals import pre_delete, post_save
from django.dispatch.dispatcher import receiver
from django.forms.models import model_to_dict
from django.utils.formats import date_format, time_format
from django.utils.functional import cached_property
from django.utils.safestring import mark_safe
from django.utils.text import Truncator
from django.utils.translation import ugettext as _


from rdrf.helpers.utils import check_calculation
from rdrf.helpers.utils import format_date, is_alphanumeric, parse_iso_datetime
from rdrf.events.events import EventType

from rdrf.forms.dsl.validator import DSLValidator
from rdrf.forms.fields.jsonb import DataField
from rdrf.helpers.registry_features import RegistryFeatures
from rdrf.helpers.cde_data_types import CDEDataTypes


logger = logging.getLogger(__name__)


class InvalidQuestionnaireError(Exception):
    pass


def new_style_questionnaire(registry):
    for form_model in registry.forms:
        if form_model.questionnaire_questions:
            if len(form_model.questionnaire_list) > 0:
                return True
    return False


class SectionManager(models.Manager):

    def get_by_natural_key(self, code):
        return self.get(code=code)

    def get_by_comma_separated_codes(self, codes):
        return self.filter(code__in=[s.strip() for s in codes.split(",")])


class Section(models.Model):
    objects = SectionManager()

    """
    A group of fields that appear on a form as a unit
    """
    code = models.CharField(max_length=100, unique=True)
    display_name = models.CharField(max_length=200)
    questionnaire_display_name = models.CharField(max_length=200, blank=True)
    elements = models.TextField()
    allow_multiple = models.BooleanField(
        default=False, help_text="Allow extra items to be added")
    extra = models.IntegerField(
        blank=True, null=True, help_text="Extra rows to show if allow_multiple checked")
    questionnaire_help = models.TextField(blank=True)

    def natural_key(self):
        return (self.code, )

    def __str__(self):
        return self.code

    def get_elements(self):
        return [code.strip() for code in self.elements.split(",")]

    @property
    def cde_models(self):
        codes = self.get_elements()
        qs = CommonDataElement.objects.filter(code__in=codes)
        cdes = {cde.code: cde for cde in qs}
        return [cdes[code] for code in codes]

    def clean(self):
        errors = {}
        elements = self.get_elements()
        codes = set(elements)

        if elements and len(elements) != len(codes):
            raise ValidationError("Section [%s] code - section contains duplicate CDEs" % self.code)

        qs = CommonDataElement.objects.filter(code__in=codes)
        missing = sorted(codes - set(qs.values_list("code", flat=True)))

        if missing:
            errors["elements"] = [
                ValidationError(
                    "section %s refers to CDE with code %s which doesn't exist" %
                    (self.display_name, code)) for code in missing]

        if not is_alphanumeric(self.code):
            raise ValidationError(
                "Section [%s] code - only letters and numbers are allowed !" %
                self.code)

        if errors:
            raise ValidationError(errors)


class RegistryManager(models.Manager):

    def get_by_natural_key(self, code):
        return self.get(code=code)


class RegistryType:
    NORMAL = 1                 # no exposed contexts - all forms stored in a default context
    HAS_CONTEXTS = 2               # supports additional contexts but has no context form groups defined
    HAS_CONTEXT_GROUPS = 3  # registry has context form groups defined


class Registry(models.Model):
    objects = RegistryManager()

    class Meta:
        verbose_name_plural = "registries"

    name = models.CharField(max_length=80)
    code = models.CharField(max_length=10, unique=True)
    desc = models.TextField()
    splash_screen = models.TextField()
    patient_splash_screen = models.TextField(blank=True, null=True)
    version = models.CharField(max_length=20, blank=True)
    # a section which holds registry specific patient information
    patient_data_section = models.ForeignKey(Section, on_delete=models.CASCADE, null=True, blank=True)
    # metadata is a dictionary
    # keys ( so far):
    # "visibility" : [ element, element , *] allows GUI elements to be shown in demographics form for a given registry but not others
    # a dictionary of configuration data -  GUI visibility
    metadata_json = models.TextField(blank=True)

    def natural_key(self):
        return (self.code, )

    def add_feature(self, feature):
        metadata = self.metadata
        features = metadata.get("features", [])
        if feature not in features:
            features.append(feature)
            metadata["features"] = features
            self.metadata_json = json.dumps(metadata)

    def remove_feature(self, feature):
        metadata = self.metadata
        features = metadata.get("features", [])
        features.remove(feature)
        metadata["features"] = features
        self.metadata_json = json.dumps(metadata)

    @property
    def features(self):
        return self.metadata.get("features", [])

    @features.setter
    def features(self, features):
        metadata = self.metadata
        metadata["features"] = features
        self.metadata_json = json.dumps(metadata)

    @property
    def registry_type(self):
        if not self.has_feature(RegistryFeatures.CONTEXTS):
            return RegistryType.NORMAL
        elif ContextFormGroup.objects.filter(registry=self).count() == 0:
            return RegistryType.HAS_CONTEXTS
        else:
            return RegistryType.HAS_CONTEXT_GROUPS

    @property
    def diagnosis_code(self):
        # used by verification workflow
        return self.metadata.get("diagnosis_code", None)

    @property
    def has_groups(self):
        return self.registry_type == RegistryType.HAS_CONTEXT_GROUPS

    @property
    def is_normal(self):
        return self.registry_type == RegistryType.NORMAL

    @property
    def metadata(self):
        if self.metadata_json:
            try:
                return json.loads(self.metadata_json)
            except ValueError:
                logger.error("Registry %s has invalid json metadata: data = '%s" %
                             (self, self.metadata_json))
                return {}
        else:
            return {}

    def get_metadata_item(self, item):
        try:
            return self.metadata[item]
        except KeyError:
            return True

    @property
    def questionnaire(self):
        try:
            return RegistryForm.objects.get(registry=self, is_questionnaire=True)
        except RegistryForm.DoesNotExist:
            return None
        except RegistryForm.MultipleObjectsReturned:
            return None

    @property
    def generated_questionnaire_name(self):
        return "GeneratedQuestionnaireFor%s" % self.code

    @property
    def questionnaire_section_prefix(self):
        return "GenQ" + self.code

    @property
    def patient_fields(self):
        """
        Registry specific fields for the demographic form
        """
        from rdrf.forms.dynamic.field_lookup import FieldFactory
        field_pairs = []  # list of pairs of cde and field object
        if self.patient_data_section:
            patient_cde_models = self.patient_data_section.cde_models
            for cde_model in patient_cde_models:
                field_factory = FieldFactory(self, None, self.patient_data_section, cde_model)
                field = field_factory.create_field()
                field_pairs.append((cde_model, field))
        # The fields were appearing in the "reverse" order, hence this
        field_pairs.reverse()
        return field_pairs

    @property
    def specific_fields_section_title(self):
        if self.patient_data_section:
            return self.patient_data_section.display_name

    def _progress_cdes(self, progress_type="diagnosis"):
        # returns list of triples (form_model, section_model, cde_model)
        results = []
        for form_model in self.forms:
            completion_cde_codes = [cde.code for cde in form_model.complete_form_cdes.all()]
            for section_model in form_model.section_models:
                for cde_model in section_model.cde_models:
                    if cde_model.code in completion_cde_codes:
                        results.append((form_model, section_model, cde_model))
        return results

    @property
    def diagnosis_progress_cde_triples(self):
        return self._progress_cdes()

    @property
    def has_diagnosis_progress_defined(self):
        return len(self.diagnosis_progress_cde_triples) > 0

    def _generated_section_questionnaire_code(self, form_name, section_code):
        return self.questionnaire_section_prefix + form_name + section_code

    def generate_questionnaire(self):
        logger.info("starting to generate questionnaire for %s" % self)
        if not new_style_questionnaire(self):
            logger.info(
                "This reqistry is not exposing any questionnaire questions - nothing to do")
            return
        questions = []
        for form in self.forms:
            for sectioncode_dot_cdecode in form.questionnaire_list:
                section_code, cde_code = sectioncode_dot_cdecode.split(".")
                questions.append((form.name, section_code, cde_code))

        from collections import OrderedDict
        section_map = OrderedDict()

        for form_name, section_code, cde_code in questions:
            section_key = (form_name, section_code)

            if section_key in section_map:
                section_map[section_key].append(cde_code)
            else:
                section_map[section_key] = [cde_code]

        generated_questionnaire_form_name = self.generated_questionnaire_name

        # get rid of any existing generated sections
        for section in Section.objects.all():
            if section.code.startswith(self.questionnaire_section_prefix):
                section.delete()

        generated_section_codes = []

        section_ordering_map = {}

        for (form_name, original_section_code) in section_map:
            # generate sections
            try:
                original_section = Section.objects.get(code=original_section_code)
            except Section.DoesNotExist:
                raise InvalidQuestionnaireError(
                    "section with code %s doesn't exist!" % original_section_code)

            qsection = Section()
            qsection.code = self._generated_section_questionnaire_code(
                form_name, original_section_code)
            qsection.questionnaire_help = original_section.questionnaire_help
            try:
                original_form = RegistryForm.objects.get(registry=self, name=form_name)
            except RegistryForm.DoesNotExist:
                raise InvalidQuestionnaireError("form with name %s doesn't exist!" % form_name)

            if not original_section.questionnaire_display_name:
                qsection.display_name = original_form.questionnaire_name + \
                    " - " + original_section.display_name
            else:
                qsection.display_name = original_form.questionnaire_name + \
                    " - " + original_section.questionnaire_display_name

            qsection.allow_multiple = original_section.allow_multiple
            qsection.extra = 0
            qsection.elements = ",".join(
                [cde_code for cde_code in section_map[(form_name, original_section_code)]])
            qsection.save()
            logger.info("created section %s containing cdes %s" %
                        (qsection.code, qsection.elements))
            generated_section_codes.append(qsection.code)

            section_ordering_map[form_name + "." + original_section_code] = qsection.code

        ordered_codes = []

        for f in self.forms:
            for s in f.get_sections():
                k = f.name + "." + s
                if k in section_ordering_map:
                    ordered_codes.append(section_ordering_map[k])

        patient_info_section = self._get_patient_info_section()

        generated_questionnaire_form, created = RegistryForm.objects.get_or_create(
            registry=self,
            name=generated_questionnaire_form_name,
            sections=patient_info_section + "," + self._get_patient_address_section() + "," + ",".join(ordered_codes)
        )
        generated_questionnaire_form.registry = self
        generated_questionnaire_form.is_questionnaire = True
        logger.info("created questionnaire form %s" % generated_questionnaire_form.name)
        generated_questionnaire_form.sections = patient_info_section + \
            "," + self._get_patient_address_section() + "," + ",".join(ordered_codes)
        generated_questionnaire_form.save()

        logger.info("finished generating questionnaire for registry %s" % self.code)

    def _get_patient_info_section(self):
        return "PatientData"

    def _get_patient_address_section(self):
        return "PatientDataAddressSection"

    @property
    def generic_sections(self):
        return [self._get_patient_info_section(), self._get_patient_address_section()]

    @property
    def generic_cdes(self):
        codes = []
        for generic_section_code in self.generic_sections:
            generic_section_model = Section.objects.get(code=generic_section_code)
            codes.extend(generic_section_model.get_elements())
        return codes

    def __str__(self):
        return "%s (%s)" % (self.name, self.code)

    def as_json(self):
        return dict(
            obj_id=self.id,
            name=self.name,
            code=self.code
        )

    @property
    def forms(self):
        return [f for f in RegistryForm.objects.filter(registry=self).order_by('position')]

    def has_feature(self, feature):
        if "features" in self.metadata:
            return feature in self.metadata["features"]
        else:
            return False

    def clean(self):
        self._check_registry_code()
        self._check_metadata()
        self._check_dupes()

    def _check_dupes(self):
        dupes = [r for r in Registry.objects.all() if r.code.lower() == self.code.lower() and r.pk != self.pk]
        names = " ".join(["%s %s" % (r.code, r.name) for r in dupes])
        if len(dupes) > 0:
            raise ValidationError(
                "Code %s already exists ( ignore case) in: %s" %
                (self.code, names))

    def _check_registry_code(self):
        if not is_alphanumeric(self.code):
            raise ValidationError(
                "Registry [%s] code - only letters and numbers are allowed !" %
                self.code
            )

    @property
    def context_name(self):
        try:
            return self.metadata['context_name']
        except KeyError:
            return "Context"

    @property
    def default_context_form_group(self):
        for cfg in ContextFormGroup.objects.filter(registry=self):
            if cfg.is_default:
                return cfg

    @property
    def free_forms(self):
        # return form models which do not below to any form group
        cfgs = ContextFormGroup.objects.filter(registry=self)
        owned_form_ids = [form_model.pk for cfg in cfgs.all() for form_model in cfg.forms]

        forms = sorted([form_model for form_model in RegistryForm.objects.filter(registry=self) if
                        form_model.pk not in owned_form_ids and not form_model.is_questionnaire],
                       key=lambda form: form.position)

        return forms

    @property
    def fixed_form_groups(self):
        return [cfg for cfg in ContextFormGroup.objects.filter(
            registry=self, context_type="F").order_by("sort_order", "is_default", "name")]

    @property
    def multiple_form_groups(self):
        return [cfg for cfg in ContextFormGroup.objects.filter(
            registry=self, context_type="M").order_by("sort_order", "name")]

    def _check_metadata(self):
        if self.metadata_json == "":
            return True
        try:
            value = json.loads(self.metadata_json)
            if not isinstance(value, dict):
                raise ValidationError("metadata json field should be a valid json dictionary")
        except ValueError:
            raise ValidationError("metadata json field should be a valid json dictionary")

    @property
    def proms_system_url(self):
        try:
            return self.metadata["proms_system_url"]
        except KeyError:
            return None

    def _registration_check(self, event_types, check_all=False):
        registration_enabled = self.has_feature(RegistryFeatures.REGISTRATION)
        registration_notifications_qs = (
            EmailNotification.objects.filter(
                registry=self,
                disabled=False,
                description__in=event_types
            )
        )
        if check_all:
            return registration_enabled and registration_notifications_qs.count() == len(event_types)
        else:
            return registration_enabled and registration_notifications_qs.exists()

    def registration_allowed(self):
        return self._registration_check(EventType.REGISTRATION_TYPES)

    def carer_registration_allowed(self):
        return self._registration_check(EventType.CARER_REGISTRATION_TYPES, check_all=True)

    def has_email_notification(self, event_type):
        return self._registration_check([event_type])


def get_owner_choices():
    """
    Get choices for CDE owner drop down.
    Used to get the list of classes which CDEs can be attached to.
    UNUSED means this CDE will not be used to construct any forms in the registry.

    """
    # for display_name, owner_model_func in settings.CDE_MODEL_MAP.items():
    #     owner_class_name = owner_model_func().__name__
    #     choices.append((owner_class_name, display_name))

    return [("UNUSED", "UNUSED"), ("USED", "USED")]


class CDEPermittedValueGroup(models.Model):
    code = models.CharField(max_length=250, primary_key=True)

    @cached_property
    def as_dict(self):
        d = {}
        d["code"] = self.code
        d["values"] = []
        for value in CDEPermittedValue.objects.filter(pv_group=self):
            value_dict = {}
            value_dict["code"] = value.code
            value_dict["value"] = value.value
            value_dict["questionnaire_value"] = value.questionnaire_value
            value_dict["desc"] = value.desc
            value_dict["position"] = value.position
            d["values"].append(value_dict)
        return d

    @cached_property
    def cde_values_dict(self):
        return {
            r['code']: r['value'] for r in CDEPermittedValue.objects.filter(pv_group=self).values('code', 'value')
        }

    def members(self, get_code=True):
        if get_code:
            att = "code"
        else:
            att = "value"

        return [
            getattr(
                v,
                att) for v in CDEPermittedValue.objects.filter(
                pv_group=self).order_by('position')]

    # interface used by proms

    @property
    def options(self):
        return [{"code": pv.code, "text": pv.value} for pv in CDEPermittedValue.objects.filter(pv_group=self).order_by('position')]

    def __str__(self):
        return "PVG %s containing %d items" % (self.code, len(self.members()))


class CDEPermittedValue(models.Model):
    id = models.AutoField(primary_key=True)
    code = models.CharField(max_length=30)
    value = models.CharField(max_length=256)
    questionnaire_value = models.CharField(max_length=256, null=True, blank=True)
    desc = models.TextField(null=True)
    pv_group = models.ForeignKey(CDEPermittedValueGroup, related_name='permitted_value_set', on_delete=models.CASCADE)
    position = models.IntegerField(null=True, blank=True)

    class Meta:
        unique_together = (('pv_group', 'code'),)

    def pvg_link(self):
        url = reverse('admin:rdrf_cdepermittedvaluegroup_change', args=(self.pv_group.code,))
        return mark_safe("<a href='%s'>%s</a>" % (url, self.pv_group.code))

    pvg_link.short_description = 'Permitted Value Group'

    def questionnaire_value_formatted(self):
        if not self.questionnaire_value:
            return mark_safe("<i><font color='red'>Not set</font></i>")
        return mark_safe("<font color='green'>%s</font>" % self.questionnaire_value)

    questionnaire_value_formatted.short_description = 'Questionnaire Value'

    def position_formatted(self):
        if not self.position:
            return mark_safe("<i><font color='red'>Not set</font></i>")
        return mark_safe("<font color='green'>%s</font>" % self.position)

    position_formatted.short_description = 'Order position'

    def __str__(self):
        return "Member of %s" % self.pv_group.code


class CommonDataElement(models.Model):

    DATA_TYPE_CHOICES = [
        (CDEDataTypes.BOOL, 'Boolean'),
        (CDEDataTypes.CALCULATED, 'Calculated'),
        (CDEDataTypes.DATE, 'Date'),
        (CDEDataTypes.DURATION, 'Duration'),
        (CDEDataTypes.EMAIL, 'Email'),
        (CDEDataTypes.FILE, 'File'),
        (CDEDataTypes.FLOAT, 'Float'),
        (CDEDataTypes.INTEGER, 'Integer'),
        (CDEDataTypes.RANGE, 'Range'),
        (CDEDataTypes.STRING, 'String'),
        (CDEDataTypes.TIME, 'Time'),
    ]

    code = models.CharField(max_length=30, primary_key=True)
    name = models.CharField(max_length=250, blank=False, help_text="Label for field in form")
    desc = models.TextField(blank=True, help_text="origin of field")
    datatype = models.CharField(choices=DATA_TYPE_CHOICES, max_length=50, help_text="type of field", default=CDEDataTypes.STRING)
    instructions = models.TextField(
        blank=True, help_text="Used to indicate help text for field")
    pv_group = models.ForeignKey(
        CDEPermittedValueGroup,
        null=True,
        blank=True,
        help_text="If a range, indicate the Permissible Value Group",
        on_delete=models.CASCADE)
    allow_multiple = models.BooleanField(
        default=False, help_text="If a range, indicate whether multiple selections allowed")
    max_length = models.IntegerField(
        blank=True, null=True, help_text="Length of field - only used for character fields")
    max_value = models.DecimalField(
        blank=True,
        null=True,
        max_digits=10,
        decimal_places=2,
        help_text="Only used for numeric fields")
    min_value = models.DecimalField(
        blank=True,
        null=True,
        max_digits=10,
        decimal_places=2,
        help_text="Only used for numeric fields")
    is_required = models.BooleanField(
        default=False, help_text="Indicate whether field is non-optional")
    pattern = models.CharField(
        max_length=50,
        blank=True,
        help_text="Regular expression to validate string fields (optional)")
    widget_name = models.CharField(
        max_length=80,
        blank=True,
        help_text="If a special widget required indicate here - leave blank otherwise",
    )
    widget_settings = models.TextField(
        blank=True,
        help_text="If the widget needs additional settings add them here")
    calculation = models.TextField(
        blank=True,
        help_text="Calculation in javascript. Use context.CDECODE to refer to other CDEs. Must use context.result to set output")
    questionnaire_text = models.TextField(
        blank=True,
        help_text="The text to use in any public facing questionnaires/registration forms")

    important = models.BooleanField(default=False, help_text="Indicate whether the field should be emphasised with a green asterisk")

    def __str__(self):
        return "CDE %s:%s" % (self.code, self.name)

    class Meta:
        verbose_name = 'Data Element'
        verbose_name_plural = 'Data Elements'

    def get_range_members(self, get_code=True):
        """
        if get_code false return the display value
        not the code
        """
        if self.pv_group:
            return self.pv_group.members(get_code=get_code)
        else:
            return None

    def get_value(self, stored_value):
        if stored_value == "NaN":
            # the DataTable was not escaping this value and interpreting it as NaN
            return None
        elif self.datatype.lower() == CDEDataTypes.DATE:
            try:
                return parse_iso_datetime(stored_value).date()
            except ValueError:
                return None
        return stored_value

    def get_display_value(self, stored_value, permitted_values_map=None):
        if stored_value is None:
            return ""
        elif stored_value == "NaN":
            # the DataTable was not escaping this value and interpreting it as NaN
            return ":NaN"
        elif self.pv_group:
            # if a range, return the display value
            try:
                if isinstance(stored_value, list):
                    return stored_value
                if permitted_values_map:
                    display_value = permitted_values_map[(stored_value, self.pv_group_id)]
                else:
                    display_value = self.pv_group.cde_values_dict[stored_value]
                return display_value
            except Exception as ex:
                logger.error("bad value for cde %s %s: %s" % (self.code,
                                                              stored_value,
                                                              ex))
        elif self.datatype.lower() == CDEDataTypes.DATE:
            try:
                return parse_iso_datetime(stored_value).date()
            except ValueError:
                return ""

        if stored_value == "NaN":
            # the DataTable was not escaping this value and interpreting it as NaN
            return ":NaN"

        return stored_value

    def clean(self):
        # this was causing issues with form progress completion cdes record
        # todo update the way form progress completion cdes are recorded to
        # only use code not cde.name!

        if "." in self.name:
            raise ValidationError(
                "CDE %s  name error '%s' has dots - this causes problems please remove" %
                (self.code, self.name))

        if not is_alphanumeric(self.code):
            raise ValidationError(
                "CDE [%s] code - only letters and numbers are allowed !" %
                self.code)

        # check javascript calculation for naughty code
        if self.calculation.strip():
            err = check_calculation(self.calculation).strip()
            if err:
                raise ValidationError({
                    "calculation": [ValidationError(e) for e in err.split("\n")]
                })

        if self.allow_multiple and self.widget_name == 'RadioSelect':
            raise ValidationError({
                'widget_name': [_("RadioSelect is not a valid choice if multiple values are allowed !")]
            })

        if self.datatype == CDEDataTypes.RANGE and not self.pv_group:
            raise ValidationError({
                'pv_group': [_("You need to have a Permissible Value Group set when using the range datatype !")]
            })

    def save(self, *args, **kwargs):
        if self.widget_name is not None:
            self.widget_name = self.widget_name.strip()
        if self.widget_name == 'SliderWidget' and self.min_value and self.max_value:
            settings = {
                "min": float(self.min_value),
                "max": float(self.max_value)
            }
            if not self.widget_settings:
                self.widget_settings = json.dumps(settings)
            else:
                existing = json.loads(self.widget_settings)
                if "min" not in existing and "max" not in existing:
                    existing.update(settings)
                    self.widget_settings = json.dumps(existing)
        super().save(*args, **kwargs)


class CdePolicy(models.Model):
    registry = models.ForeignKey(Registry, on_delete=models.CASCADE)
    cde = models.ForeignKey(CommonDataElement, on_delete=models.CASCADE)
    groups_allowed = models.ManyToManyField(Group, blank=True)
    condition = models.TextField(blank=True)

    def is_allowed(self, user_groups, patient_model=None, is_superuser=False):
        if is_superuser:
            return True
        for ug in user_groups:
            if ug in self.groups_allowed.all():
                if patient_model:
                    return self.evaluate_condition(patient_model)
                else:
                    return True

    class Meta:
        verbose_name = "CDE Policy"
        verbose_name_plural = "CDE Policies"

    def evaluate_condition(self, patient_model):
        if not self.condition:
            return True
        # need to think about safety here

        context = {"patient": patient_model.as_dto()}
        result = eval(self.condition, {"__builtins__": None}, context)
        return result


class RegistryFormManager(models.Manager):

    def get_by_natural_key(self, registry_code, name):
        return self.get(registry__code=registry_code, name=name)

    def get_by_registry(self, registry):
        return self.model.objects.filter(registry__id__in=registry)


class RegistryForm(models.Model):
    """
    A representation of a form ( a bunch of sections)
    """
    objects = RegistryFormManager()

    registry = models.ForeignKey(Registry, on_delete=models.CASCADE)
    name = models.CharField(max_length=80,
                            help_text="Internal name used by system: Alphanumeric, no spaces")
    display_name = models.CharField(max_length=200,
                                    blank=True,
                                    null=True,
                                    help_text="Form Name displayed to users")
    header = models.TextField(blank=True)
    questionnaire_display_name = models.CharField(max_length=80, blank=True)
    sections = models.TextField(help_text="Comma-separated list of sections")
    is_questionnaire = models.BooleanField(
        default=False, help_text="Check if this form is questionnaire form for it's registry")
    is_questionnaire_login = models.BooleanField(
        default=False,
        help_text="If the form is a questionnaire, is it accessible only by logged in users?",
        verbose_name="Questionnaire Login Required")
    position = models.PositiveIntegerField(default=0)
    questionnaire_questions = models.TextField(
        blank=True, help_text="Comma-separated list of sectioncode.cdecodes for questionnnaire")
    complete_form_cdes = models.ManyToManyField(CommonDataElement, blank=True)
    groups_allowed = models.ManyToManyField(Group, blank=True)
    applicability_condition = models.TextField(blank=True,
                                               null=True,
                                               help_text="E.g. patient.deceased == True")
    conditional_rendering_rules = models.TextField(
        blank=True,
        null=True,
        help_text='''Use the conditional rendering DSL to add rules.
                     Click <a href="/forms/dsl-help" target="_blank">here</a> for more info'''
    )
    tags = ArrayField(models.CharField(max_length=20), default=list, blank=True)

    class Meta:
        ordering = ('registry', 'position')

    def natural_key(self):
        return (self.registry.code, self.name)

    def validate_unique(self, exclude=None):
        models.Model.validate_unique(self, exclude)
        if not ('registry__code' in exclude or 'name' in exclude):
            if (RegistryForm.objects.filter(registry__code=self.registry.code, name=self.name)
                                    .exclude(pk=self.pk)
                                    .exists()):
                raise ValidationError(
                    "RegistryForm with registry.code '%s' and name '%s' already exists" %
                    (self.registry.code, self.name))

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def open(self):
        return self.groups_allowed.count() == 0

    @property
    def restricted(self):
        return not self.open

    @property
    def login_required(self):
        return self.is_questionnaire_login

    @property
    def questionnaire_name(self):
        from rdrf.helpers.utils import de_camelcase
        if self.questionnaire_display_name:
            return self.questionnaire_display_name
        else:
            return de_camelcase(self.name)

    def __str__(self):
        return "%s %s Form comprising %s" % (self.registry, self.name, self.sections)

    def get_sections(self):
        return [s.strip() for s in self.sections.split(",")]

    @property
    def questionnaire_list(self):
        """
        returns a list of sectioncode.cde_code strings
        E.g. [ "sectionA.cdecode23", "sectionB.code100" , ...]
        """
        return [q.strip() for q in self.questionnaire_questions.split(",") if q.strip()]

    @property
    def section_models(self):
        return Section.objects.get_by_comma_separated_codes(self.sections)

    def in_questionnaire(self, section_code, cde_code):
        return f"{section_code}.{cde_code}" in self.questionnaire_list

    @property
    def has_progress_indicator(self):
        return len(self.complete_form_cdes.values_list()) > 0

    def link(self, patient_model):
        from rdrf.helpers.utils import FormLink
        return FormLink(patient_model.pk, self.registry, self).url

    @property
    def nice_name(self):
        from rdrf.helpers.utils import de_camelcase
        try:
            return self.display_name if self.display_name else de_camelcase(self.name)
        except BaseException:
            return self.name

    @property
    def has_progress(self):
        # does this form define form progress ( completion) cdes?
        return self.complete_form_cdes.count() > 0

    def get_link(self, patient_model, context_model=None):
        if context_model is None:
            return reverse(
                'registry_form',
                args=(
                    self.registry.code,
                    self.id,
                    patient_model.id))
        else:
            return reverse(
                'registry_form',
                args=(
                    self.registry.code,
                    self.id,
                    patient_model.id,
                    context_model.id))

    def _check_completion_cdes(self, complete_form_cdes, section_codes):
        completion_cdes = set(cde.code for cde in complete_form_cdes)
        section_models = Section.objects.get_by_comma_separated_codes(section_codes)
        current_cdes = set(
            code
            for section_model in section_models
            for code in section_model.get_elements())

        extra = completion_cdes - current_cdes

        if len(extra) > 0:
            msg = Truncator(", ".join(sorted(extra))).chars(250)
            raise ValidationError("Some completion cdes don't exist on the form: %s" % msg)

    def clean(self):
        if not is_alphanumeric(self.name):
            msg = "Only letters and numbers are allowed for form name: Use CamelCase to make GUI display the name as" + \
                "Camel Case, instead."
            raise ValidationError({'name': msg})

        if self.conditional_rendering_rules:
            DSLValidator(self.conditional_rendering_rules, self).check_rules()

        self._check_sections()

    def _check_sections(self):
        for section_code in self.get_sections():
            try:
                Section.objects.get(code=section_code)
            except Section.DoesNotExist:
                raise ValidationError("Section %s does not exist!" % section_code)

    def applicable_to(self, patient):
        # 2 levels of restriction:
        # by patient type , set up in the registry metadata
        # and further by a dynamic condition
        # thus we can have forms applicable to all carrier patients
        # ( patient_type = carrier) and also
        # deceased patients, say. ( for MTM)
        # the default case is True - ie all forms are applicable to a patient
        from rdrf.helpers.utils import applicable_forms

        if patient is None:
            return False

        if not patient.in_registry(self.registry.code):
            return False
        else:
            allowed_forms = [f.name for f in applicable_forms(self.registry, patient)]
            if self.name not in allowed_forms:
                return False

        # In allowed list for patient type, but is there a patient condition also?

        if not self.applicability_condition:
            return True

        evaluation_context = {"patient": patient.as_dto()}

        try:
            is_applicable = eval(self.applicability_condition,
                                 {"__builtins__": None},
                                 evaluation_context)
        except BaseException:
            # allows us to filter out forms for patients
            # which are not related with the assumed structure
            # in the supplied condition
            return False

        return is_applicable


class Wizard(models.Model):
    registry = models.CharField(max_length=50)
    forms = models.TextField(help_text="A comma-separated list of forms")
    # idea
    # rules for "decision tree"
    # These could be as simple as the following:
    # A wizard is a way of coordinating the asking of questions and evaluating
    # answers -

    #  E.g. in pseudo-code ( we either present a gui to create rules like this
    # or write an interpreter
    #  present form1.
    #  if form1.section1.cde2 == 3 and section2.cde >6  then present form3
    #  if form1.section2.cde2 == 4 then present form5
    #  else present form6
    #
    rules = models.TextField(help_text="Rules")


class QuestionnaireResponse(models.Model):
    registry = models.ForeignKey(Registry, on_delete=models.CASCADE)
    date_submitted = models.DateTimeField(auto_now_add=True)
    processed = models.BooleanField(default=False)
    patient_id = models.IntegerField(
        blank=True,
        null=True,
        help_text="The id of the patient created from this response, if any")

    def __str__(self):
        return "%s (%s)" % (self.registry, self.processed)

    @property
    def name(self):
        return self._get_patient_field(
            "CDEPatientGivenNames") + " " + self._get_patient_field("CDEPatientFamilyName")

    @property
    def date_of_birth(self):
        # time was being included from questionnaire for some data: e.g. '1918-08-01T00:00:00'
        dob_string = self._get_patient_field("CDEPatientDateOfBirth")
        if not dob_string:
            return ""

        try:
            return parse_iso_datetime(dob_string).date()
        except ValueError:
            return ""

    def _get_patient_field(self, patient_field):
        from rdrf.db.dynamic_data import DynamicDataWrapper
        wrapper = DynamicDataWrapper(self)

        if not self.has_mongo_data:
            raise ObjectDoesNotExist

        questionnaire_form_name = RegistryForm.objects.get(
            registry=self.registry, is_questionnaire=True).name

        value = wrapper.get_nested_cde(
            self.registry.code,
            questionnaire_form_name,
            "PatientData",
            patient_field)

        if value is None:
            return ""

        return value

    @property
    def has_mongo_data(self):
        from rdrf.db.dynamic_data import DynamicDataWrapper
        wrapper = DynamicDataWrapper(self)
        return wrapper.has_data(self.registry.code)

    @property
    def data(self):
        # return the filled in questionnaire data
        from rdrf.db.dynamic_data import DynamicDataWrapper
        wrapper = DynamicDataWrapper(self)
        return wrapper.load_dynamic_data(self.registry.code, "cdes", flattened=False)


def appears_in(cde, registry, registry_form, section):
    if section.code not in registry_form.get_sections():
        return False
    elif registry_form.name not in [f.name for f in RegistryForm.objects.filter(registry=registry)]:
        return False
    else:
        return cde.code in section.get_elements()


class MissingData(object):
    pass


class Notification(models.Model):
    from_username = models.CharField(max_length=80)
    to_username = models.CharField(max_length=80)
    created = models.DateTimeField(auto_now_add=True)
    message = models.TextField()
    link = models.CharField(max_length=100, default="")
    seen = models.BooleanField(default=False)


class ConsentConfiguration(models.Model):

    ENABLED = 'enabled'
    DISABLED = 'disabled'
    REQUIRED = 'required'

    SIGNATURE_CHOICES = ((ENABLED, ENABLED), (DISABLED, DISABLED), (REQUIRED, REQUIRED))

    registry = models.OneToOneField(Registry, related_name="consent_configuration", on_delete=models.CASCADE)
    esignature = models.CharField(choices=SIGNATURE_CHOICES, default=DISABLED, max_length=16)
    consent_locked = models.BooleanField(default=False)

    @property
    def signature_disabled(self):
        return self.esignature == self.DISABLED

    @property
    def signature_required(self):
        return self.esignature == self.REQUIRED

    @property
    def signature_enabled(self):
        return self.esignature == self.ENABLED


class ConsentSectionManager(models.Manager):

    def get_by_natural_key(self, registry_code, code):
        return self.get(registry__code=registry_code, code=code)


class ConsentSection(models.Model):
    objects = ConsentSectionManager()

    code = models.CharField(max_length=20)
    section_label = models.CharField(max_length=100)
    registry = models.ForeignKey(Registry, related_name="consent_sections", on_delete=models.CASCADE)
    information_link = models.CharField(max_length=100, blank=True, null=True)
    information_text = models.TextField(blank=True, null=True)
    # eg "patient.age > 6 and patient.age" < 10
    applicability_condition = models.TextField(blank=True)
    validation_rule = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, blank=True, null=True)
    last_updated_at = models.DateTimeField(auto_now=True, blank=True, null=True)

    def natural_key(self):
        return (self.registry.code, self.code)

    def validate_unique(self, exclude=None):
        models.Model.validate_unique(self, exclude)
        if not ('registry__code' in exclude or 'code' in exclude):
            if (ConsentSection.objects.filter(registry__code=self.registry.code, code=self.code)
                                      .exclude(pk=self.pk)
                                      .exists()):
                raise ValidationError(
                    "ConsentSection with registry.code '%s' and code '%s' already exists" %
                    (self.registry.code, self.code))

    @property
    def latest_update(self):
        updates = [self.last_updated_at] + [q.last_updated_at for q in self.questions.all()]
        valid_updates = [u for u in updates if u is not None]
        if valid_updates:
            latest = max(valid_updates)
            return f"{date_format(latest)} {time_format(latest)}"
        return None

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def applicable_to(self, patient):
        if patient is None:
            return True

        if not patient.in_registry(self.registry.code):
            return False
        else:
            # if no restriction return True
            if not self.applicability_condition:
                return True

            from registry.patients.models import ParentGuardian
            self_patient = False
            try:
                ParentGuardian.objects.get(self_patient=patient)
                self_patient = True
            except ParentGuardian.DoesNotExist:
                pass

            function_context = {"patient": patient.as_dto(), "self_patient": self_patient.as_dto()}

            is_applicable = eval(
                self.applicability_condition, {"__builtins__": None}, function_context)

            return is_applicable

    def is_valid(self, answer_dict):
        """
        does the supplied question_code --> answer map
        satisfy the validation rule for this section
        :param answer_dict: map of question codes to bool
        :return: True or False depending on validation rule
        """
        if not self.validation_rule:
            return True

        function_context = {}

        for consent_question_code in answer_dict:
            answer = answer_dict[consent_question_code]
            function_context[consent_question_code] = answer

        # codes not in dict are set to false ..

        for question_model in self.questions.all():
            if question_model.code not in answer_dict:
                function_context[question_model.code] = False

        try:

            result = eval(self.validation_rule, {"__builtins__": None}, function_context)
            if result not in [True, False, None]:
                return False

            return result
        except Exception as ex:
            logger.error(
                "Error evaluating consent section %s rule %s context %s error %s" %
                (self.code, self.validation_rule, function_context, ex))

            return False

    def __str__(self):
        return "Consent Section %s" % self.section_label

    @property
    def link(self):
        if self.information_link:
            return reverse('documents', args=(self.information_link,))
        else:
            return ""

    @property
    def form_info(self):
        from django.forms import BooleanField
        info = {}
        info["section_label"] = "%s %s" % (self.registry.code, self.section_label)
        info["information_link"] = self.information_link
        consent_fields = []
        for consent_question_model in self.questions.all().order_by("position"):
            consent_fields.append(BooleanField(label=consent_question_model.question_label))
        info["consent_fields"] = consent_fields
        return info


class ConsentQuestionManager(models.Manager):

    def get_by_natural_key(self, section_code, code):
        return self.get(section__code=section_code, code=code)


class ConsentQuestion(models.Model):
    objects = ConsentQuestionManager()

    code = models.CharField(max_length=20)
    position = models.IntegerField(blank=True, null=True)
    section = models.ForeignKey(ConsentSection, related_name="questions", on_delete=models.CASCADE)
    question_label = models.TextField()
    instructions = models.TextField(blank=True)
    questionnaire_label = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, blank=True, null=True)
    last_updated_at = models.DateTimeField(auto_now=True, blank=True, null=True)

    class Meta:
        unique_together = ('section', 'code')

    def natural_key(self):
        return (self.section.code, self.code)

    def create_field(self, patient, viewing_user):
        from django.forms import BooleanField
        field = BooleanField(
            label=self.question_label,
            required=False,
            help_text=self.instructions,
        )
        if viewing_user.is_clinician:
            consent_value = self.consentvalue_set.filter(patient=patient).first()
            if not consent_value:
                title = _('Never consented')
            else:
                action_date = consent_value.first_save or consent_value.last_updated
                date = date_format(action_date)
                if consent_value.answer:
                    title = _('Consented on {date}'.format(date=date))
                else:
                    title = _('Revoked on {date}'.format(date=date))
            field.widget.attrs['title'] = title
        return field

    @property
    def field_key(self):
        registry_model = self.section.registry
        consent_section_model = self.section
        return "customconsent_%s_%s_%s" % (registry_model.pk, consent_section_model.pk, self.pk)

    def label(self, on_questionnaire=False):
        if on_questionnaire and self.questionnaire_label:
            return self.questionnaire_label
        else:
            return self.question_label

    def __str__(self):
        return "%s" % self.question_label


class ConsentRule(models.Model):
    # restrictions on what a user can do with a patient
    # based on patient consent
    # e.g. restrict clinical users from seeing patients' data
    # if the patient has not given explicit consent
    CAPABILITIES = (('see_patient', 'See Patient'),)
    registry = models.ForeignKey(Registry, on_delete=models.CASCADE)
    user_group = models.ForeignKey(Group, on_delete=models.CASCADE)
    capability = models.CharField(max_length=50, choices=CAPABILITIES)
    consent_question = models.ForeignKey(ConsentQuestion, on_delete=models.CASCADE)
    enabled = models.BooleanField(default=True)


class DemographicFields(models.Model):
    FIELD_CHOICES = []
    READONLY = 1
    HIDDEN = 2
    STATUS_CHOICES = [(READONLY, "Read only"), (HIDDEN, "Hidden")]
    registry = models.ForeignKey(Registry, on_delete=models.CASCADE)
    groups = models.ManyToManyField(Group, related_name='demographic_fields')
    field = models.CharField(max_length=50, choices=FIELD_CHOICES)
    status = models.IntegerField(choices=STATUS_CHOICES, default=2)
    is_section = models.BooleanField(null=False, default=False)

    class Meta:
        verbose_name_plural = "Demographic Fields"
        unique_together = ('registry', 'field')
        ordering = ('registry', '-is_section', 'field', 'status')


class EmailTemplate(models.Model):
    language = models.CharField(max_length=2, choices=settings.ALL_LANGUAGES)
    description = models.TextField()
    subject = models.CharField(max_length=50)
    body = models.TextField()

    def __str__(self):
        return "%s (%s)" % (self.description, dict(settings.LANGUAGES)[self.language])


class EmailNotification(models.Model):
    EMAIL_NOTIFICATIONS = (
        (EventType.ACCOUNT_LOCKED, "Account Locked"),
        (EventType.OTHER_CLINICIAN, "Other Clinician"),
        (EventType.NEW_PATIENT, "New Patient Registered"),
        (EventType.NEW_PATIENT_PARENT, "New Patient Registered (Parent)"),
        (EventType.ACCOUNT_VERIFIED, "Account Verified"),
        (EventType.PASSWORD_EXPIRY_WARNING, "Password Expiry Warning"),
        (EventType.REMINDER, "Reminder"),
        (EventType.CLINICIAN_SIGNUP_REQUEST, "Clinician Signup Request"),
        (EventType.CLINICIAN_ACTIVATION, "Clinician Activation"),
        (EventType.CLINICIAN_SELECTED, "Clinician Selected"),
        (EventType.PARTICIPANT_CLINICIAN_NOTIFICATION, "Participant Clinician Notification"),
        (EventType.PATIENT_CONSENT_CHANGE, "Patient Consent Change"),
        (EventType.NEW_CARER, "Primary Caregiver Registered"),
        (EventType.CARER_INVITED, "Primary Caregiver Invited"),
        (EventType.CARER_ASSIGNED, "Primary Caregiver Assigned"),
        (EventType.CARER_ACTIVATED, "Primary Caregiver Activated"),
        (EventType.CARER_DEACTIVATED, "Primary Caregiver Deactivated"),
        (EventType.SURVEY_REQUEST, "Survey Request"),
        (EventType.DUPLICATE_PATIENT_SET, "Duplicate Patient Set"),
        (EventType.CLINICIAN_ASSIGNED, "Clinician Assigned"),
        (EventType.CLINICIAN_UNASSIGNED, "Clinician Unassigned"),
    )

    description = models.CharField(max_length=100, choices=EMAIL_NOTIFICATIONS)
    registry = models.ForeignKey(Registry, on_delete=models.CASCADE)
    email_from = models.EmailField(null=True, blank=True, help_text='Leave empty for default email address')
    recipient = models.CharField(max_length=100, null=True, blank=True)
    group_recipient = models.ForeignKey(Group, null=True, blank=True, on_delete=models.SET_NULL)
    email_templates = models.ManyToManyField(EmailTemplate)
    disabled = models.BooleanField(null=False, blank=False, default=False)

    def __str__(self):
        return "%s (%s)" % (self.description, self.registry.code.upper())


class EmailNotificationHistory(models.Model):
    date_stamp = models.DateTimeField(auto_now_add=True)
    language = models.CharField(max_length=10)
    email_notification = models.ForeignKey(EmailNotification, on_delete=models.CASCADE)
    template_data = models.TextField(null=True, blank=True)

    class Meta:
        verbose_name_plural = 'Email Notification History'


class RDRFContextError(Exception):
    pass


class RDRFCtxManager(models.Manager):

    def get_queryset(self):
        # do NOT include inactive (soft-deleted) contexts
        return super().get_queryset().filter(active=True)

    def all_contexts(self):
        # shows soft-deleted contexts also
        return super().get_queryset().all()

    def inactive_contexts(self):
        return self.all_contexts().filter(active=False)

    def get_for_patient(self, patient, registry):
        return self.get_queryset().filter(
            registry=registry,
            content_type=ContentType.objects.get_for_model(patient),
            object_id=patient.pk,
        )


class RDRFContext(models.Model):
    registry = models.ForeignKey(Registry, on_delete=models.CASCADE)
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    context_form_group = models.ForeignKey("ContextFormGroup",
                                           null=True,
                                           blank=True,
                                           on_delete=models.SET_NULL)
    object_id = models.PositiveIntegerField()
    content_object = GenericForeignKey('content_type', 'object_id')
    created_at = models.DateTimeField(auto_now_add=True)
    last_updated = models.DateTimeField(auto_now=True)
    last_updated_by = models.ForeignKey('groups.CustomUser', blank=True, null=True, on_delete=models.SET_NULL)
    display_name = models.CharField(max_length=80, blank=True, null=True)
    active = models.BooleanField(default=True, blank=False)
    objects = RDRFCtxManager()

    def __str__(self):
        return "%s %s" % (self.display_name, self.created_at)

    def clean(self):
        if not self.display_name:
            raise ValidationError("RDRF Context must have a display name")

    @property
    def context_name(self):
        if self.context_form_group:
            if self.context_form_group.naming_scheme == "C":
                return self._get_name_from_cde()
            else:
                return self.context_form_group.name  # E.G. Assessment or Visit - used for display
        else:
            try:
                return self.registry.metadata["context_name"]
            except KeyError:
                return "Context"

    def _get_name_from_cde(self):
        if not self.context_form_group.naming_cde_to_use:
            return "Follow Up"
        cde_path = self.context_form_group.naming_cde_to_use
        form_name, section_code, cde_code = cde_path.split("/")
        cde_value = self.content_object.get_form_value(self.registry.code,
                                                       form_name,
                                                       section_code,
                                                       context_id=self.pk)
        return cde_value

    @property
    def is_multi_context(self):
        return (
            self.context_form_group.context_type == "M"
            if self.context_form_group
            else False
        )


class ContextFormGroup(models.Model):
    CONTEXT_TYPES = [("F", "Fixed"), ("M", "Multiple")]
    NAMING_SCHEMES = [("D", "Automatic - Date"),
                      ("N", "Automatic - Number"),
                      ("M", "Manual - Free Text"),
                      ("C", "CDE - Nominate CDE to use")]
    ORDERING_TYPES = [("C", "Creation Time"),
                      ("N", "Name")]

    registry = models.ForeignKey(Registry,
                                 related_name="context_form_groups",
                                 on_delete=models.CASCADE)
    context_type = models.CharField(max_length=1, default="F", choices=CONTEXT_TYPES)
    name = models.CharField(max_length=80)
    naming_scheme = models.CharField(max_length=1, default="D", choices=NAMING_SCHEMES)
    is_default = models.BooleanField(default=False)
    naming_cde_to_use = models.CharField(max_length=80, blank=True, null=True)
    ordering = models.CharField(max_length=1, default="C", choices=ORDERING_TYPES)
    sort_order = models.PositiveIntegerField(null=False, blank=False, default=1)

    @property
    def forms(self):
        def sort_func(form):
            return form.position

        return sorted([item.registry_form for item in self.items.all()],
                      key=sort_func)

    def __str__(self):
        return self.name

    @property
    def direct_name(self):
        """
        If there is only one form in the group , show _its_ name
        """
        if not self.supports_direct_linking:
            return self.name

        return self.forms[0].nice_name

    @property
    def supports_direct_linking(self):
        return len(self.forms) == 1

    @property
    def is_ordered_by_name(self):
        return self.ordering == "N"

    @property
    def is_ordered_by_creation(self):
        return self.ordering == "C"

    @property
    def is_fixed(self):
        return self.context_type == "F"

    @property
    def is_multiple(self):
        return self.context_type == "M"

    def get_default_name(self, patient_model, context_model=None):
        if self.naming_scheme == "M":
            return "Modules"
        elif self.naming_scheme == "D" and context_model is not None:
            d = context_model.created_at
            s = d.strftime("%d-%b-%Y")
            t = d.strftime("%I:%M:%S %p")
            return "%s/%s %s" % (self.name, s, t)
        elif self.naming_scheme == "N":
            registry_model = self.registry
            contexts = RDRFContext.objects.get_for_patient(patient_model, registry_model)
            existing_count = contexts.filter(context_form_group=self).count()
            next_number = existing_count + 1
            return "%s/%s" % (self.name, next_number)
        elif self.naming_scheme == "C":
            return "Unused"  # user will see value from cde when context is created
        else:
            return "Modules"

    def get_value_from_cde(self, patient_model, context_model):
        form_name, section_code, cde_code = self.naming_cde_to_use.split("/")
        try:
            cde_value = patient_model.get_form_value(self.registry.code,
                                                     form_name,
                                                     section_code,
                                                     cde_code,
                                                     context_id=context_model.pk)

            cde_model = CommonDataElement.objects.get(code=cde_code)
            return cde_model.get_value(cde_value)
        except KeyError:
            # value not filled out yet
            return None

    def get_name_from_cde(self, patient_model, context_model):
        if not self.naming_cde_to_use:
            return self.get_default_name(patient_model, context_model)
        form_name, section_code, cde_code = self.naming_cde_to_use.split("/")
        try:
            cde_value = patient_model.get_form_value(self.registry.code,
                                                     form_name,
                                                     section_code,
                                                     cde_code,
                                                     context_id=context_model.pk)

            cde_model = CommonDataElement.objects.get(code=cde_code)
            # This does not actually do type conversion for dates -
            # it just looks up range display codes.
            display_value = cde_model.get_display_value(cde_value)
            if isinstance(display_value, datetime.date):
                display_value = format_date(display_value)
            return display_value
        except KeyError:
            # value not filled out yet
            return "NOT SET"

    def get_ordering_value(self, patient_model, context_model):
        from rdrf.helpers.utils import MinType
        bottom = MinType()
        if context_model.display_name:
            display_name = context_model.display_name
        else:
            display_name = "Not set"

        if self.is_ordered_by_name:
            if self.naming_scheme == "C":
                try:
                    value = self.get_value_from_cde(patient_model, context_model)

                    if value is None:
                        return bottom
                    else:
                        return value
                except BaseException:
                    return bottom
            return display_name

        if self.is_ordered_by_creation:
            return context_model.created_at

        return bottom

    @property
    def naming_info(self):
        if self.naming_scheme == "M":
            return "Display name will default to 'Modules' if left blank"
        elif self.naming_scheme == "N":
            return "Display name will default to <Context Type Name>/<Sequence Number>"
        elif self.naming_scheme == "D":
            return "Display name will default to <Context Type Name>/<created_at date>"
        elif self.naming_scheme == "C":
            return "Display name will be equal to the value of a nominated CDE"
        else:
            return "Display name will default to 'Modules' if left blank"

    def clean(self):
        defaults = ContextFormGroup.objects.filter(registry=self.registry,
                                                   is_default=True).exclude(pk=self.pk)
        num_defaults = defaults.count()

        if num_defaults > 0 and self.is_default:
            raise ValidationError("Only one Context Form Group can be the default")
        if num_defaults == 0 and not self.is_default:
            raise ValidationError("One Context Form Group must be chosen as the default")

        if self.naming_scheme == "C" and self._valid_naming_cde_to_use(self.naming_cde_to_use) is None:
            raise ValidationError("Invalid naming cde: Should be form name/section code/cde code where all codes must exist")

    def _valid_naming_cde_to_use(self, naming_cde_to_use):
        validation_message = "Invalid naming cde: Should be form name/section code/cde code where all codes must exist"
        if naming_cde_to_use:
            try:
                naming_cde_expression = naming_cde_to_use.split("/")
                form_name, section_code, cde_code = naming_cde_expression
            except ValueError:
                raise ValidationError(validation_message)

            try:
                form_model = RegistryForm.objects.get(registry=self.registry,
                                                      name=form_name)
            except RegistryForm.DoesNotExist:
                raise ValidationError(validation_message)

            section_model = self._get_section_model(section_code, form_model)
            if section_model is None:
                raise ValidationError(validation_message)

            cde_model = self._get_cde_model(cde_code, section_model)
            if cde_model is None:
                raise ValidationError(validation_message)

            return form_name, section_code, cde_code
        return None

    def _get_section_model(self, section_code, form_model):
        for section_model in form_model.section_models:
            if section_model.code == section_code:
                return section_model

    def _get_cde_model(self, cde_code, section_model):
        for cde_model in section_model.cde_models:
            if cde_model.code == cde_code:
                return cde_model

    def patient_can_add(self, patient_model):
        """
        can this patient add a context of my type?
        """
        if self.context_type == "M":
            return True
        else:
            # fixed - is there one already?
            return not (
                RDRFContext.objects
                           .get_for_patient(patient_model, self.registry)
                           .filter(context_form_group=self)
                           .exists()
            )

    def get_add_action(self, patient_model):
        if self.patient_can_add(patient_model):
            num_forms = len(self.forms)
            # Direct link to form if num forms is 1 ( handler redirects transparently)
            from rdrf.helpers.utils import de_camelcase as dc
            action_title = "Add %s" % dc(
                self.forms[0].name) if num_forms == 1 else "Add %s" % dc(
                self.name)

            if not self.supports_direct_linking:
                # We can't go directly to the form - so we first land on the add context view, which on save
                # creates the context with links to the forms provided in that context
                # after save
                action_link = reverse("context_add", args=(self.registry.code,
                                                           str(patient_model.pk),
                                                           str(self.pk)))

            else:
                form_model = self.forms[0]
                # provide a link to the create view for a clinical form
                # url(r"^(?P<registry_code>\w+)/forms/(?P<form_id>\w+)/(?P<patient_id>\d+)/add/?$",

                action_link = reverse("form_add", args=(self.registry.code,
                                                        form_model.pk,
                                                        patient_model.pk,
                                                        'add'))

            return action_link, action_title
        else:
            return None


class ContextFormGroupItem(models.Model):
    context_form_group = models.ForeignKey(ContextFormGroup,
                                           related_name="items",
                                           on_delete=models.CASCADE)
    registry_form = models.ForeignKey(RegistryForm, on_delete=models.CASCADE)


class ClinicalDataQuerySet(models.QuerySet):
    def collection(self, registry_code, collection):
        qs = self.filter(registry_code=registry_code, collection=collection, active=True)
        return qs.order_by("pk")

    def active(self):
        return self.filter(active=True)

    def find(self, obj=None, context_id=None, **query):
        q = {}
        if obj is not None:
            q["django_id"] = obj.id
            q["django_model"] = obj.__class__.__name__
        if context_id is not None:
            q["context_id"] = context_id
        for attr, value in query.items():
            q["data__" + attr] = value
        return self.filter(**q)

    def data(self):
        return self.values_list("data", flat=True)


class ClinicalData(models.Model):
    """
    MongoDB collections in Django.
    """
    COLLECTIONS = (
        ("cdes", "cdes"),
        ("history", "history"),
        ("progress", "progress"),
        ("registry_specific_patient_data", "registry_specific_patient_data"),
    )

    registry_code = models.CharField(max_length=10, db_index=True)
    collection = models.CharField(max_length=50, db_index=True, choices=COLLECTIONS)
    data = DataField()
    django_id = models.IntegerField(db_index=True, default=0)
    django_model = models.CharField(max_length=80, db_index=True, default="Patient")
    context_id = models.IntegerField(db_index=True, blank=True, null=True)
    active = models.BooleanField(
        default=True, help_text="Indicate whether an entity is active or not")
    created_at = models.DateTimeField(auto_now_add=True, null=True)
    last_updated_at = models.DateTimeField(auto_now=True, null=True)
    last_updated_by = models.IntegerField(db_index=True, blank=True, null=True)

    objects = ClinicalDataQuerySet.as_manager()

    @classmethod
    def create(cls, obj, **kwargs):
        instance = cls(**kwargs)
        instance.data["django_model"] = obj.__class__.__name__
        instance.data["django_id"] = obj.id
        instance.django_id = obj.id
        instance.django_model = obj.__class__.__name__
        if "context_id" in kwargs:
            instance.context_id = kwargs["context_id"]
        return instance

    def __str__(self):
        return json.dumps(model_to_dict(self), indent=2)

    def cde_val(self, form_name, section_code, cde_code):
        forms = self.data.get("forms", [])
        form_map = {f.get("name"): f for f in forms}
        sections = form_map.get(form_name, {}).get("sections", [])
        section_map = {s.get("code"): s for s in sections}
        cdes = section_map.get(section_code, {}).get("cdes", [])
        cde_map = {c.get("code"): c for c in cdes}
        return cde_map.get(cde_code, {}).get("value")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def clean(self):
        self._clean_registry_code()
        self._clean_data()

    def _clean_registry_code(self):
        if not Registry.objects.filter(code=self.registry_code).exists():
            raise ValidationError(
                {"registry_code": "Registry %s does not exist" % self.registry_code})

    modjgo_schema = None
    #  need tp fix this path rdrf/db/schemas/modjgo.yaml
    modjgo_schema_file = os.path.join(os.path.dirname(__file__), "schemas/modjgo.yaml")

    def validate(self, collection, data):
        return

        # to do fithis
        if not self.modjgo_schema:
            try:
                with open(self.modjgo_schema_file) as f:
                    self.modjgo_schema = yaml.load(f.read(), Loader=yaml.FullLoader)
            except BaseException:
                logger.exception("Error reading %s" % self.modjgo_schema_file)

        if self.modjgo_schema:
            jsonschema.validate({collection: data}, self.modjgo_schema)

    lax_validation = True

    def _clean_data(self):
        try:
            self.validate(self.collection, self.data)
        except jsonschema.ValidationError as e:
            if self.lax_validation:
                logger.warning("Failed to validate: %s" % e)
            else:
                raise ValidationError({"data": e})


def file_upload_to(instance, filename):
    return "/".join(filter(bool, [
        instance.registry_code,
        instance.section_code or "_",
        instance.cde_code, filename]))


class CDEFile(models.Model):
    """
    A file record which is referenced by id within the patient's
    dynamic data dictionary.

    The form and section fields are optional for files belonging to
    registry-specific fields.

    See filestorage.py for usage of this model.
    """
    registry_code = models.CharField(max_length=10)
    uploaded_by = models.ForeignKey('groups.CustomUser', blank=True, null=True, on_delete=models.PROTECT)
    patient = models.ForeignKey('patients.Patient', blank=True, null=True, on_delete=models.PROTECT)
    form_name = models.CharField(max_length=80, blank=True)
    section_code = models.CharField(max_length=100, blank=True)
    cde_code = models.CharField(max_length=30, blank=True)
    item = models.FileField(upload_to=file_upload_to, max_length=300)
    filename = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=255, null=True, blank=True)

    def __str__(self):
        return self.item.name


@receiver(pre_delete, sender=CDEFile)
def fileuploaditem_delete(sender, instance, **kwargs):
    instance.item.delete(False)


@receiver(post_save, sender=Registry)
def registry_post_save(sender, instance, **kwargs):
    from registry.patients.models import PatientStage
    from rdrf.initial_data.patient_stage import init_registry_stages_and_rules

    if instance.has_feature(RegistryFeatures.STAGES):
        if not PatientStage.objects.filter(registry=instance).exists():
            init_registry_stages_and_rules(instance)


class FileStorage(models.Model):
    """
    This model is used only when the database file storage backend is
    enabled. These exact columns are required by the backend code.
    """
    name = models.CharField(primary_key=True, max_length=255)
    data = models.BinaryField()
    size = models.IntegerField(default=0)


class FormTitle(models.Model):
    FORM_TITLE_CHOICES = (
        ("Demographics", "Demographics"),
        ("Consents", "Consents"),
        ("Clinician", "Clinician"),
        ("Proms", "Proms"),
        ("Family linkage", "Family Linkage")
    )
    registry = models.ForeignKey(Registry, on_delete=models.CASCADE)
    groups = models.ManyToManyField(
        Group,
        help_text="Users of these groups will see the custom title instead of the default one"
    )
    default_title = models.CharField(choices=FORM_TITLE_CHOICES, blank=False, null=False, max_length=50)
    custom_title = models.CharField(max_length=50)
    order = models.PositiveIntegerField(help_text="When the user with multiple groups matches more than 1 customisation the title with the lower order number will be displayed.")

    class Meta:
        ordering = ('registry', 'default_title', 'order')

    @property
    def group_names(self):
        return ', '.join(group.name for group in self.groups.order_by('name').all())


class BlacklistedMimeType(models.Model):
    mime_type = models.CharField(max_length=256, unique=True)
    description = models.TextField()

    def __str__(self):
        return f"{self.mime_type} - {self.description}"

    class Meta:
        verbose_name = "Disallowed mime type"
