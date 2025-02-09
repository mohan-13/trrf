from csp.decorators import csp_update
from django.shortcuts import render, get_object_or_404
from django.template.loader import render_to_string
from django.views.generic.base import View, TemplateView
from django.template.context_processors import csrf
from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import PermissionDenied
from django.contrib import messages
from django.http import HttpResponse, FileResponse, JsonResponse
from django.http import HttpResponseRedirect, HttpResponseNotFound
from django.forms.formsets import formset_factory
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.safestring import mark_safe

from rdrf.models.definition.models import RegistryForm, Registry, QuestionnaireResponse, ContextFormGroup
from rdrf.models.definition.models import CDEFile, Section, CommonDataElement
from registry.patients.models import Patient, ParentGuardian, PatientSignature
from rdrf.forms.dynamic.dynamic_forms import create_form_class_for_section
from rdrf.db.dynamic_data import DynamicDataWrapper
from rdrf.db.filestorage import virus_checker_result
from django.http import Http404
from rdrf.forms.dsl.code_generator import CodeGenerator
from rdrf.forms.file_upload import wrap_fs_data_for_form
from rdrf.forms.file_upload import wrap_file_cdes
from rdrf.db import filestorage
from rdrf.helpers.utils import de_camelcase, location_name, is_multisection, make_index_map
from rdrf.helpers.utils import parse_iso_date
from rdrf.views.decorators.patient_decorators import patient_questionnaire_access
from rdrf.forms.navigation.wizard import NavigationWizard, NavigationFormType
from rdrf.models.definition.models import RDRFContext

from rdrf.forms.consent_forms import CustomConsentFormGenerator
from rdrf.helpers.registry_features import RegistryFeatures
from rdrf.helpers.utils import consent_status_for_patient


from rdrf.db.contexts_api import RDRFContextManager
from rdrf.db.contexts_api import RDRFContextError
from explorer.utils import create_field_values


from django.shortcuts import redirect
from django.forms.models import inlineformset_factory
from registry.patients.models import PatientConsent
from registry.patients.admin_forms import PatientConsentFileForm, PatientSignatureForm
from django.utils.translation import ugettext as _

import json
import os
from collections import OrderedDict
from django.conf import settings
from rdrf.services.rpc.actions import ActionExecutor
from rdrf.helpers.utils import FormLink, consent_check
from rdrf.forms.dynamic.dynamic_forms import create_form_class_for_consent_section
from rdrf.forms.dynamic.form_changes import FormChangesExtractor
from rdrf.forms.progress.form_progress import FormProgress

from rdrf.forms.navigation.locators import PatientLocator
from rdrf.forms.components import RDRFContextLauncherComponent
from rdrf.forms.components import RDRFPatientInfoComponent
from rdrf.security.security_checks import (
    security_check_user_patient, can_sign_consent,
    get_object_or_permission_denied
)

from registry.patients.patient_stage_flows import get_registry_stage_flow

from rdrf.admin_forms import CommonDataElementAdminForm
from rdrf.forms.widgets.widgets import get_widgets_for_data_type
from rdrf.helpers.cde_data_types import CDEDataTypes
from rdrf.helpers.view_helper import FileErrorHandlingMixin
from rdrf.security.mixins import StaffMemberRequiredMixin

from aws_xray_sdk.core import xray_recorder

import logging

logger = logging.getLogger(__name__)


class RDRFContextSwitchError(Exception):
    pass


class CustomConsentHelper(object):

    def __init__(self, registry_model):
        self.registry_model = registry_model
        self.custom_consent_errors = {}
        self.custom_consent_data = None
        self.custom_consent_keys = []
        self.custom_consent_wrappers = []
        self.error_count = 0

    def create_custom_consent_wrappers(self):

        for consent_section_model in self.registry_model.consent_sections.order_by("code"):
            consent_form_class = create_form_class_for_consent_section(
                self.registry_model, consent_section_model)
            consent_form = consent_form_class(data=self.custom_consent_data)
            consent_form_wrapper = ConsentFormWrapper(consent_section_model.section_label,
                                                      consent_form,
                                                      consent_section_model)

            self.custom_consent_wrappers.append(consent_form_wrapper)

    def get_custom_consent_keys_from_request(self, request):
        self.custom_consent_data = {}
        for key in request.POST:
            if key.startswith("customconsent_"):
                self.custom_consent_data[key] = request.POST[key]
                self.custom_consent_keys.append(key)

    def check_for_errors(self):
        for custom_consent_wrapper in self.custom_consent_wrappers:
            if not custom_consent_wrapper.is_valid():
                self.custom_consent_errors[
                    custom_consent_wrapper.label] = [
                    error_message for error_message in custom_consent_wrapper.errors]
                self.error_count += custom_consent_wrapper.num_errors

    def load_dynamic_data(self, dynamic_data):
        # load data from Mongo
        self.custom_consent_data = dynamic_data.get("custom_consent_data", None)


class SectionInfo(object):
    """
    Info to store a section ( and create section forms)

    Used so we save everything _after_ all sections have validated.
    Also the file upload links weren't being created post save for the POST response
    because the forms had already been instantiated and "wrapped" too early.

    """

    def __init__(self,
                 section_code,
                 patient_wrapper,
                 is_multiple,
                 registry_code,
                 collection_name,
                 data,
                 index_map=None,
                 form_set_class=None,
                 form_class=None,
                 prefix=None):
        self.section_code = section_code
        self.patient_wrapper = patient_wrapper
        self.is_multiple = is_multiple
        self.registry_code = registry_code
        self.collection_name = collection_name
        self.data = data
        self.index_map = index_map
        # if this section is not a multisection this form class is used to create the form
        self.form_class = form_class
        # otherwise we create a formset using these
        self.form_set_class = form_set_class
        self.prefix = prefix

    def save(self):
        if not self.is_multiple:
            self.patient_wrapper.save_dynamic_data(
                self.registry_code, self.collection_name, self.data)
        else:
            self.patient_wrapper.save_dynamic_data(self.registry_code,
                                                   self.collection_name,
                                                   self.data,
                                                   multisection=True,
                                                   index_map=self.index_map)

    def recreate_form_instance(self):
        # called when all sections on a form are valid
        # We do this to create a form instance which has correct links to uploaded files
        current_data = self.patient_wrapper.load_dynamic_data(self.registry_code, "cdes")
        if self.is_multiple:
            # the cleaned data from the form submission
            dynamic_data = self.data[self.section_code]
        else:
            dynamic_data = self.data

        wrapped_data = wrap_file_cdes(
            self.registry_code,
            dynamic_data,
            current_data,
            multisection=self.is_multiple)

        if self.is_multiple:
            form_instance = self.form_set_class(initial=wrapped_data, prefix=self.prefix)
        else:
            form_instance = self.form_class(dynamic_data, initial=wrapped_data)

        return form_instance


class FormView(View):

    def __init__(self, *args, **kwargs):
        # when set to True in integration testing, switches off unsupported messaging middleware
        self.template = None
        self.registry = None
        self.dynamic_data = {}
        self.registry_form = None
        self.form_id = None
        self.patient_id = None
        self.user = None
        self.rdrf_context = None
        self.show_multisection_delete_checkbox = True

        super(FormView, self).__init__(*args, **kwargs)

    def _get_registry(self, registry_code):
        try:
            return Registry.objects.get(code=registry_code)

        except Registry.DoesNotExist:
            raise Http404("Registry %s does not exist" % registry_code)

    def _get_dynamic_data(self, registry_code=None, rdrf_context_id=None,
                          model_class=Patient, id=None):
        obj = model_class.objects.get(pk=id)
        dyn_obj = DynamicDataWrapper(obj, rdrf_context_id=rdrf_context_id)
        dynamic_data = dyn_obj.load_dynamic_data(registry_code, "cdes")
        return dynamic_data

    def set_rdrf_context(self, patient_model, context_id):
        # Ensure we always have a context , otherwise bail
        self.rdrf_context = None
        try:
            if context_id is None:
                if self.registry.has_feature(RegistryFeatures.CONTEXTS):
                    raise RDRFContextError(
                        "Registry %s supports contexts but no context id  passed in url" %
                        self.registry)
                else:
                    self.rdrf_context = self.rdrf_context_manager.get_or_create_default_context(
                        patient_model)
            else:
                self.rdrf_context = self.rdrf_context_manager.get_context(
                    context_id, patient_model)

            if self.rdrf_context is None:
                raise RDRFContextSwitchError

        except RDRFContextError as ex:
            logger.error(
                "Error setting rdrf context id %s for patient %s in %s: %s" %
                (context_id, patient_model, self.registry, ex))
            raise RDRFContextSwitchError

    def _evaluate_form_rules(self, form_rules, evaluation_context):
        from rdrf.workflows.rules_engine import RulesEvaluator
        evaluator = RulesEvaluator(form_rules, evaluation_context)
        return evaluator.get_action()

    def _enable_context_creation_after_save(self,
                                            request,
                                            registry_code,
                                            form_id,
                                            patient_id):
        # Enable only if:
        #   the form is the only member of a context form group marked as multiple
        user = request.user
        registry_model = Registry.objects.get(code=registry_code)
        form_model = RegistryForm.objects.get(id=form_id)
        patient_model = Patient.objects.get(id=patient_id)

        if not registry_model.has_feature(RegistryFeatures.CONTEXTS):
            raise Http404

        if not patient_model.in_registry(registry_model.code):
            raise Http404

        if not user.can_view(form_model):
            raise PermissionDenied

        # is this form the only member of a multiple form group?
        form_group = None
        for cfg in registry_model.multiple_form_groups:
            form_models = cfg.forms
            if len(form_models) == 1 and form_models[0].pk == form_model.pk:
                form_group = cfg
                break

        if form_group is None:
            raise Http404

        self.create_mode_config = {
            "form_group": form_group,
        }

        self.CREATE_MODE = True

    @staticmethod
    def get_form_group_name(patient_model, context):
        return context.context_form_group.get_default_name(patient_model, context)

    def init_previous_data_members(self):
        self.previous_data = None
        self.previous_versions = []
        self.has_previous_contexts = False

    def fetch_previous_data(self, changes_since_version, patient_model, registry_code):
        selected_version_name = ''
        if not self.rdrf_context:
            # create mode
            return None, selected_version_name

        previous_contexts_qs = self.rdrf_context_manager.get_previous_contexts(
            self.rdrf_context, patient_model
        )
        self.has_previous_contexts = previous_contexts_qs.exists()
        for prev_context in previous_contexts_qs:
            form_group_name = self.get_form_group_name(patient_model, prev_context)
            clinical_data = self._get_dynamic_data(
                id=patient_model.id,
                registry_code=registry_code,
                rdrf_context_id=prev_context.id
            )
            if changes_since_version and int(changes_since_version) == prev_context.id:
                self.previous_data = clinical_data
                selected_version_name = form_group_name
            self.previous_versions.append({
                "id": prev_context.id,
                "name": form_group_name,
            })
        if not self.previous_data:
            # We must have received a version that isn't valid (same context form group and previous version)
            # Returning None to avoid entering in Compare Mode
            changes_since_version = None

        return changes_since_version, selected_version_name

    def delete(self, request, registry_code, form_id, patient_id, context_id=None):
        if request.user.is_working_group_staff:
            raise PermissionDenied()
        patient_model = get_object_or_permission_denied(Patient, pk=patient_id)
        security_check_user_patient(request.user, patient_model)
        self.registry = self._get_registry(registry_code)
        if not consent_check(self.registry, request.user, patient_model, "see_patient"):
            raise PermissionDenied

        rdrf_context = get_object_or_404(RDRFContext, pk=context_id)
        if rdrf_context.is_multi_context:
            RDRFContext.objects.filter(pk=context_id).update(active=False, last_updated_by=request.user)
            dyn_obj = DynamicDataWrapper(patient_model, rdrf_context_id=context_id)
            dyn_obj.soft_delete(registry_code, request.user.id)
            result = {"result": "Context deleted !"}
            return JsonResponse(result, status=200)

        return JsonResponse({"result": "Cannot delete form !"}, status=400)

    def set_code_generator_data(self, context, empty_stubs=False):
        if empty_stubs:
            context["generated_code"] = ''
            context["visibility_handler"] = ''
            context["change_targets"] = ''
            context["generated_declarations"] = ''
        else:
            code_gen = CodeGenerator(self.registry_form.conditional_rendering_rules, self.registry_form)
            context["generated_code"] = code_gen.generate_code() or ''
            context["visibility_handler"] = code_gen.generate_visibility_handler() or ''
            context["change_targets"] = code_gen.generate_change_targets() or ''
            context["generated_declarations"] = code_gen.generate_declarations() or ''

    def registry_permissions_check(self, request, registry_code, form_id, patient_id, context_id):
        """
        Overridden in custom registries to perform registry-specific permissions checks
        """
        pass

    def get(self, request, registry_code, form_id, patient_id, context_id=None):
        xray_recorder.begin_subsegment('formview_get')
        xray_recorder.begin_subsegment('auth')
        # RDR-1398 enable a Create View which context_id of 'add' is provided
        if context_id is None:
            raise Http404
        self.CREATE_MODE = False  # Normal edit view; False means Create View and context saved AFTER validity check
        if context_id == 'add':
            self._enable_context_creation_after_save(request,
                                                     registry_code,
                                                     form_id,
                                                     patient_id)

        if request.user.is_working_group_staff:
            raise PermissionDenied()
        self.user = request.user
        self.form_id = form_id
        self.patient_id = patient_id

        patient_model = get_object_or_permission_denied(Patient, pk=patient_id)
        security_check_user_patient(request.user, patient_model)
        self.registry_permissions_check(request, registry_code, form_id, patient_id, context_id)

        self.registry = self._get_registry(registry_code)

        if not consent_check(self.registry, self.user, patient_model, "see_patient"):
            raise PermissionDenied

        self.registry_form = self.get_registry_form(form_id)
        if not self.user.can_view(self.registry_form):
            raise PermissionDenied
        xray_recorder.end_subsegment()

        xray_recorder.begin_subsegment("contexts")
        self.rdrf_context_manager = RDRFContextManager(self.registry)

        try:
            if not self.CREATE_MODE:
                self.set_rdrf_context(patient_model, context_id)
        except RDRFContextSwitchError:
            return HttpResponseRedirect("/")
        xray_recorder.end_subsegment()

        xray_recorder.begin_subsegment("data")
        request.session['num_retries'] = settings.SESSION_REFRESH_MAX_RETRIES
        self.init_previous_data_members()
        changes_since_version = request.GET.get("changes_since_version")
        if changes_since_version:
            try:
                int(changes_since_version)
            except ValueError:
                changes_since_version = None
        selected_version_name = ''
        if self.CREATE_MODE:
            rdrf_context_id = "add"
            self.dynamic_data = None
        else:
            rdrf_context_id = self.rdrf_context.pk
            self.dynamic_data = self._get_dynamic_data(id=patient_id,
                                                       registry_code=registry_code,
                                                       rdrf_context_id=rdrf_context_id)
            changes_since_version, selected_version_name = self.fetch_previous_data(changes_since_version, patient_model, registry_code)
        xray_recorder.end_subsegment()

        if not self.registry_form.applicable_to(patient_model):
            return HttpResponseRedirect(reverse("patientslisting"))

        xray_recorder.begin_subsegment("template")
        context_launcher = RDRFContextLauncherComponent(request.user,
                                                        self.registry,
                                                        patient_model,
                                                        self.registry_form.name,
                                                        self.rdrf_context,
                                                        registry_form=self.registry_form)

        context = self._build_context(user=request.user, patient_model=patient_model, changes_since_version=changes_since_version)
        context["location"] = location_name(self.registry_form, self.rdrf_context)
        # we provide a "path" to the header field which contains an embedded Django template
        context["header"] = self.registry_form.header
        context["header_expression"] = "rdrf://model/RegistryForm/%s/header" % self.registry_form.pk
        context["settings"] = settings
        context["is_multi_context"] = self.rdrf_context.is_multi_context if self.rdrf_context else False
        patient_info_component = RDRFPatientInfoComponent(self.registry, patient_model, request.user)

        if not self.CREATE_MODE:
            context["CREATE_MODE"] = False
            context["show_print_button"] = True
            context["patient_info"] = patient_info_component.html
            context["show_archive_button"] = request.user.can_archive
            context["not_linked"] = not patient_model.is_linked
            context["archive_patient_url"] = patient_model.get_archive_url(
                self.registry) if request.user.can_archive else ""
        else:
            context["CREATE_MODE"] = True
            context["show_print_button"] = False
            context["show_archive_button"] = False

        wizard = NavigationWizard(self.user,
                                  self.registry,
                                  patient_model,
                                  NavigationFormType.CLINICAL,
                                  context_id,
                                  self.registry_form)

        context["next_form_link"] = wizard.next_link
        context["context_id"] = context_id
        context["previous_form_link"] = wizard.previous_link
        context["context_launcher"] = context_launcher.html

        if request.user.is_parent:
            context['parent'] = ParentGuardian.objects.get(user=request.user)

        context["my_contexts_url"] = patient_model.get_contexts_url(self.registry)
        context["context_id"] = rdrf_context_id
        context["delete_form_url"] = reverse(
            "registry_form",
            kwargs={"registry_code": registry_code, "patient_id": patient_id, "form_id": form_id, "context_id": context_id}
        ) if context_id != 'add' else ''

        conditional_rendering_disabled = changes_since_version or \
            self.registry.has_feature(RegistryFeatures.CONDITIONAL_RENDERING_DISABLED)
        self.set_code_generator_data(context, empty_stubs=conditional_rendering_disabled)

        context["selected_version_name"] = selected_version_name

        xray_recorder.end_subsegment()

        xray_recorder.begin_subsegment("render")
        response = self._render_context(request, context)
        xray_recorder.end_subsegment()

        xray_recorder.end_subsegment()
        return response

    def _render_context(self, request, context):
        context.update(csrf(request))
        return render(request, self._get_template(), context)

    def _get_field_ids(self, form_class):
        # the ids of each cde on the form
        return ",".join(form_class().fields.keys())

    def post(self, request, registry_code, form_id, patient_id, context_id=None):
        xray_recorder.begin_subsegment('formview_post')
        xray_recorder.begin_subsegment("auth")
        if context_id is None:
            raise Http404
        all_errors = []
        progress_dict = {}

        self.CREATE_MODE = False  # Normal edit view; False means Create View and context saved AFTER validity check
        sections_to_save = []  # when a section is validated it is added to this list
        all_sections_valid = True
        if context_id == 'add':
            # The following switches on CREATE_MODE if conditions satisfied
            self._enable_context_creation_after_save(request,
                                                     registry_code,
                                                     form_id,
                                                     patient_id)
        if request.user.is_superuser:
            pass
        elif request.user.is_working_group_staff or request.user.has_perm("rdrf.form_%s_is_readonly" % form_id):
            raise PermissionDenied()

        self.user = request.user

        registry = Registry.objects.get(code=registry_code)
        self.registry = registry

        patient = get_object_or_permission_denied(Patient, pk=patient_id)
        security_check_user_patient(request.user, patient)
        self.registry_permissions_check(request, registry_code, form_id, patient_id, context_id)

        if not consent_check(self.registry, request.user, patient, "see_patient"):
            raise PermissionDenied

        self.patient_id = patient_id
        xray_recorder.end_subsegment()

        xray_recorder.begin_subsegment("contexts")
        self.rdrf_context_manager = RDRFContextManager(self.registry)

        try:
            if not self.CREATE_MODE:
                self.set_rdrf_context(patient, context_id)
        except RDRFContextSwitchError:
            return HttpResponseRedirect("/")
        xray_recorder.end_subsegment()

        xray_recorder.begin_subsegment("data")
        request.session['num_retries'] = settings.SESSION_REFRESH_MAX_RETRIES

        if not self.CREATE_MODE:
            dyn_patient = DynamicDataWrapper(patient, rdrf_context_id=self.rdrf_context.pk)
        else:
            dyn_patient = DynamicDataWrapper(patient, rdrf_context_id='add')

        dyn_patient.user = request.user

        self.init_previous_data_members()
        changes_since_version, __ = self.fetch_previous_data(None, patient, registry_code)
        xray_recorder.end_subsegment()

        xray_recorder.begin_subsegment("form")
        form_obj = self.get_registry_form(form_id)
        # this allows form level timestamps to be saved
        dyn_patient.current_form_model = form_obj
        self.registry_form = form_obj

        form_display_name = form_obj.display_name if form_obj.display_name else form_obj.name
        sections, display_names, ids = self._get_sections(form_obj)
        form_section = {}
        section_element_map = {}
        total_forms_ids = {}
        initial_forms_ids = {}
        formset_prefixes = {}
        error_count = 0
        # this is used by formset plugin:
        # the full ids on form eg { "section23": ["form23^^sec01^^CDEName", ... ] , ...}
        section_field_ids_map = {}

        for section_index, s in enumerate(sections):
            section_model = Section.objects.get(code=s)
            form_class = create_form_class_for_section(
                registry,
                form_obj,
                section_model,
                injected_model="Patient",
                injected_model_id=self.patient_id,
                is_superuser=self.user.is_superuser,
                user_groups=self.user.groups.all(),
                patient_model=patient)
            if not form_class:
                continue
            section_elements = section_model.get_elements()
            section_element_map[s] = section_elements
            section_field_ids_map[s] = self._get_field_ids(form_class)

            if not section_model.allow_multiple:
                form = form_class(request.POST, files=request.FILES)
                if form.is_valid():
                    dynamic_data = form.cleaned_data
                    section_info = SectionInfo(
                        s,
                        dyn_patient,
                        False,
                        registry_code,
                        "cdes",
                        dynamic_data,
                        form_class=form_class)
                    sections_to_save.append(section_info)
                    current_data = dyn_patient.load_dynamic_data(self.registry.code, "cdes")
                    form_data = wrap_file_cdes(
                        registry_code, dynamic_data, current_data, multisection=False)
                    form_section[s] = form_class(dynamic_data, initial=form_data)
                else:
                    all_sections_valid = False
                    for e in form.errors:
                        error_count += 1
                        all_errors.append(e)

                    from rdrf.helpers.utils import wrap_uploaded_files
                    post_copy = request.POST.copy()
                    # request.POST.update(request.FILES)
                    post_copy.update(request.FILES)

                    form_section[s] = form_class(wrap_uploaded_files(
                        registry_code, post_copy), request.FILES)

            else:
                if section_model.extra:
                    extra = section_model.extra
                else:
                    extra = 0

                prefix = "formset_%s" % s
                formset_prefixes[s] = prefix
                total_forms_ids[s] = "id_%s-TOTAL_FORMS" % prefix
                initial_forms_ids[s] = "id_%s-INITIAL_FORMS" % prefix
                form_set_class = formset_factory(form_class, extra=extra, can_delete=True)
                formset = form_set_class(request.POST, files=request.FILES, prefix=prefix)
                assert formset.prefix == prefix

                if formset.is_valid():
                    dynamic_data = formset.cleaned_data  # a list of values
                    to_remove = [i for i, d in enumerate(dynamic_data) if d.get('DELETE')]
                    index_map = make_index_map(to_remove, len(dynamic_data))

                    for i in reversed(to_remove):
                        del dynamic_data[i]

                    current_data = dyn_patient.load_dynamic_data(self.registry.code, "cdes")
                    section_dict = {s: dynamic_data}
                    section_info = SectionInfo(s,
                                               dyn_patient,
                                               True,
                                               registry_code,
                                               "cdes",
                                               section_dict,
                                               index_map,
                                               form_set_class=form_set_class,
                                               prefix=prefix)

                    sections_to_save.append(section_info)
                    form_data = wrap_file_cdes(
                        registry_code,
                        dynamic_data,
                        current_data,
                        multisection=True,
                        index_map=index_map)
                    form_section[s] = form_set_class(initial=form_data, prefix=prefix)

                else:
                    all_sections_valid = False
                    for e in formset.errors:
                        error_count += 1
                        all_errors.append(e)
                    form_section[s] = form_set_class(request.POST, request.FILES, prefix=prefix)

        xray_recorder.end_subsegment()

        if all_sections_valid:
            xray_recorder.begin_subsegment("section_save")
            # Only save to the db iff all sections are valid
            # If all sections are valid, each section form instance  needs to be re-created here as other wise the links
            # to any upload files won't work
            # If any are invalid, nothing needs to be done as the forms have already been created from the form
            # submission data
            for section_info in sections_to_save:
                section_info.save()
                form_instance = section_info.recreate_form_instance()
                form_section[section_info.section_code] = form_instance

            xray_recorder.end_subsegment()

            xray_recorder.begin_subsegment("progress")
            if not self.CREATE_MODE:
                progress_dict = dyn_patient.save_form_progress(
                    registry_code, context_model=self.rdrf_context)
            xray_recorder.end_subsegment()

            xray_recorder.begin_subsegment("save_snapshot")
            # Save one snapshot after all sections have being persisted
            dyn_patient.save_snapshot(
                registry_code,
                "cdes",
                form_name=form_obj.name,
                form_user=self.request.user.username)
            xray_recorder.end_subsegment()

            xray_recorder.begin_subsegment("report_fields")
            # save report friendly field values
            try:
                logger.debug("trying to create field values for %s" % patient)
                if self.rdrf_context:
                    create_field_values(registry,
                                        patient,
                                        self.rdrf_context)
                logger.debug("created field values for patient %s" % patient)
            except Exception as ex:
                logger.debug("error creating field values: %s" % ex)
                raise
            xray_recorder.end_subsegment()

            if self.CREATE_MODE and dyn_patient.rdrf_context_id != "add":
                xray_recorder.begin_subsegment("existing_form")
                # we've created the context on the fly so no redirect to the edit view on
                # the new context
                newly_created_context = RDRFContext.objects.get(id=dyn_patient.rdrf_context_id)
                dyn_patient.save_form_progress(
                    registry_code, context_model=newly_created_context)

                try:
                    create_field_values(registry,
                                        patient,
                                        newly_created_context)
                except Exception as ex:
                    logger.debug("Error creating field values for new context: %s" % ex)

                xray_recorder.end_subsegment()
                xray_recorder.end_subsegment()  # End main subsegment
                return HttpResponseRedirect(
                    reverse(
                        'registry_form',
                        args=(
                            registry_code,
                            form_id,
                            patient.pk,
                            newly_created_context.pk)))

            if dyn_patient.rdrf_context_id == "add":
                raise Exception("Content not created")

            if registry.has_feature(RegistryFeatures.RULES_ENGINE):
                xray_recorder.begin_subsegment("rules")
                rules_block = registry.metadata.get("rules", {})
                form_rules = rules_block.get(form_obj.name, [])
                logger.debug("checking rules for %s" % form_obj.name)
                logger.debug("form_rules = %s" % form_rules)
                if len(form_rules) > 0:
                    # this may redirect or produce side effects
                    rules_evaluation_context = {"patient_model": patient,
                                                "registry_model": registry,
                                                "form_name": form_obj.name,
                                                "context_id": self.rdrf_context.pk,
                                                "clinical_data": None}
                    action_result = self._evaluate_form_rules(form_rules, rules_evaluation_context)
                    xray_recorder.end_subsegment()
                    if isinstance(action_result, HttpResponseRedirect):
                        return action_result
                else:
                    logger.debug("No evaluation rules to apply")

        xray_recorder.begin_subsegment("wizard")
        patient_name = '%s %s' % (patient.given_names, patient.family_name)
        # progress saved to progress collection in mongo
        # the data is returned also
        wizard = NavigationWizard(self.user,
                                  registry,
                                  patient,
                                  NavigationFormType.CLINICAL,
                                  context_id,
                                  form_obj)
        xray_recorder.end_subsegment()

        xray_recorder.begin_subsegment("template")
        context_launcher = RDRFContextLauncherComponent(request.user,
                                                        registry,
                                                        patient,
                                                        self.registry_form.name,
                                                        self.rdrf_context,
                                                        registry_form=self.registry_form)

        patient_info_component = RDRFPatientInfoComponent(registry, patient, request.user)

        context = {
            'CREATE_MODE': self.CREATE_MODE,
            'current_registry_name': registry.name,
            'current_form_name': form_obj.display_name if form_obj.display_name else de_camelcase(form_obj.name),
            'registry': registry_code,
            'registry_code': registry_code,
            'form_name': form_id,
            'form_display_name': form_display_name,
            'patient_id': patient_id,
            'patient_link': PatientLocator(registry, patient).link,
            'sections': sections,
            'patient_info': patient_info_component.html,
            'section_field_ids_map': section_field_ids_map,
            'section_ids': ids,
            'forms': form_section,
            'my_contexts_url': patient.get_contexts_url(self.registry),
            'display_names': display_names,
            'section_element_map': section_element_map,
            "total_forms_ids": total_forms_ids,
            "initial_forms_ids": initial_forms_ids,
            "formset_prefixes": formset_prefixes,
            "form_links": self._get_formlinks(request.user, self.rdrf_context),
            "metadata_json_for_sections": self._get_metadata_json_dict(self.registry_form),
            "has_form_progress": self.registry_form.has_progress_indicator,
            "location": location_name(self.registry_form, self.rdrf_context),
            "next_form_link": wizard.next_link,
            "not_linked": not patient.is_linked,
            "previous_form_link": wizard.previous_link,
            "context_id": context_id,
            "show_print_button": True if not self.CREATE_MODE else False,
            "archive_patient_url": patient.get_archive_url(registry) if request.user.can_archive else "",
            "show_archive_button": request.user.can_archive if not self.CREATE_MODE else False,
            "context_launcher": context_launcher.html,
            "have_dynamic_data": all_sections_valid,
            'settings': settings,
            "has_previous_data": self.has_previous_contexts,
            "previous_versions": self.previous_versions,
            "changes_since_version": changes_since_version,
        }

        if request.user.is_parent:
            context['parent'] = ParentGuardian.objects.get(user=request.user)

        if not self.registry_form.is_questionnaire:
            form_progress_map = progress_dict.get(
                self.registry_form.name + "_form_progress", {})
            if "percentage" in form_progress_map:
                progress_percentage = form_progress_map["percentage"]
            else:
                progress_percentage = 0

            context["form_progress"] = progress_percentage
            initial_completion_cdes = {cde_model.name: False for cde_model in
                                       self.registry_form.complete_form_cdes.all()}

            context["form_progress_cdes"] = progress_dict.get(
                self.registry_form.name + "_form_cdes_status", initial_completion_cdes)

        context.update(csrf(request))
        self.registry_form = self.get_registry_form(form_id)

        context["header"] = self.registry_form.header
        context["header_expression"] = "rdrf://model/RegistryForm/%s/header" % self.registry_form.pk

        if error_count == 0:
            patient.mark_changed_timestamp()

            success_message = _(f"Patient {patient_name} saved successfully. Please now use the blue arrow on the right to continue.")
            messages.add_message(request,
                                 messages.SUCCESS,
                                 success_message)
        else:
            failure_message = _(f"Patient {patient_name} not saved due to validation errors")

            messages.add_message(request,
                                 messages.ERROR,
                                 failure_message)
            context['error_messages'] = [failure_message]

        self.set_code_generator_data(
            context,
            empty_stubs=self.registry.has_feature(RegistryFeatures.CONDITIONAL_RENDERING_DISABLED)
        )
        xray_recorder.end_subsegment()

        xray_recorder.begin_subsegment("render")
        response = render(request, self._get_template(), context)
        xray_recorder.end_subsegment()

        xray_recorder.end_subsegment()
        return response

    def _get_sections(self, form):
        section_parts = form.get_sections()
        sections = []
        display_names = {}
        ids = {}
        for s in section_parts:
            try:
                sec = Section.objects.get(code=s.strip())
                display_names[s] = sec.display_name
                ids[s] = sec.id
                sections.append(s)
            except ObjectDoesNotExist:
                logger.error("Section %s does not exist" % s)
        return sections, display_names, ids

    def get_registry_form(self, form_id):
        return RegistryForm.objects.get(id=form_id)

    def _get_form_class_for_section(self, registry, registry_form, section, allowed_cdes, previous_values):
        return create_form_class_for_section(
            registry,
            registry_form,
            section,
            injected_model="Patient",
            injected_model_id=self.patient_id,
            is_superuser=self.request.user.is_superuser,
            user_groups=self.request.user.groups.all(),
            allowed_cdes=allowed_cdes,
            previous_values=previous_values)

    def _get_formlinks(self, user, context_model=None):
        container_model = self.registry
        if context_model is not None:
            if context_model.context_form_group:
                container_model = context_model.context_form_group
        if user is not None:
            return [
                FormLink(
                    self.patient_id,
                    self.registry,
                    form,
                    selected=(
                        form.name == self.registry_form.name),
                    context_model=self.rdrf_context
                ) for form in container_model.forms if not form.is_questionnaire and user.can_view(form)]
        else:
            return []

    def _build_context(self, **kwargs):
        """
        :param kwargs: extra key value pairs to be passed into the built context
        :return: a context dictionary to render the template ( all form generation done here)
        """
        user = kwargs.get("user", None)
        patient_model = kwargs.get("patient_model", None)
        sections, display_names, ids = self._get_sections(self.registry_form)
        form_section = {}
        section_element_map = {}
        total_forms_ids = {}
        initial_forms_ids = {}
        formset_prefixes = {}
        section_field_ids_map = {}
        form_links = self._get_formlinks(user, self.rdrf_context)
        if self.dynamic_data:
            if 'questionnaire_context' in kwargs:
                self.dynamic_data['questionnaire_context'] = kwargs['questionnaire_context']
            else:
                self.dynamic_data['questionnaire_context'] = 'au'
        changes_since_version = kwargs.get("changes_since_version")
        form_changes = FormChangesExtractor(self.registry_form, self.previous_data, self.dynamic_data)
        form_changes.determine_form_changes()
        allowed_cdes = form_changes.allowed_cdes if changes_since_version else []
        previous_values = form_changes.previous_values if changes_since_version else {}

        remove_sections = []
        for s in sections:
            section_model = Section.objects.get(code=s)
            form_class = self._get_form_class_for_section(
                self.registry, self.registry_form, section_model, allowed_cdes, previous_values)
            if not form_class:
                remove_sections.append(s)
                continue
            section_elements = section_model.get_elements()
            section_element_map[s] = section_elements
            section_field_ids_map[s] = self._get_field_ids(form_class)

            if not section_model.allow_multiple:
                # return a normal form
                initial_data = wrap_fs_data_for_form(self.registry, self.dynamic_data)
                form_section[s] = form_class(self.dynamic_data, initial=initial_data)

            else:
                # Ensure that we can have multiple formsets on the one page
                prefix = "formset_%s" % s
                formset_prefixes[s] = prefix
                total_forms_ids[s] = "id_%s-TOTAL_FORMS" % prefix
                initial_forms_ids[s] = "id_%s-INITIAL_FORMS" % prefix

                # return a formset
                if section_model.extra:
                    extra = section_model.extra
                else:
                    extra = 0
                form_set_class = formset_factory(
                    form_class, extra=extra, can_delete=self.show_multisection_delete_checkbox)
                has_deleted_forms = False
                deleted_index = -1
                if self.dynamic_data:
                    try:
                        # we grab the list of data items by section code not cde code
                        data = self.dynamic_data[s]
                        if changes_since_version and self.previous_data and s in self.previous_data:
                            prev_data = self.previous_data[s]
                            if len(prev_data) > len(data):
                                has_deleted_forms = True
                                deleted_index = len(data)
                                for k in range(len(data), len(prev_data)):
                                    prev_data[k]['deleted'] = True
                                    data.append(prev_data[k])
                        initial_data = wrap_fs_data_for_form(
                            self.registry, data)
                    except KeyError:
                        initial_data = [""]  # * len(section_elements)
                else:
                    # initial_data = [""] * len(section_elements)
                    initial_data = [""]  # this appears to forms

                form_section[s] = form_set_class(initial=initial_data, prefix=prefix)
                if has_deleted_forms:
                    for idx, form in enumerate(form_section[s].forms):
                        if idx >= deleted_index:
                            form.was_deleted = True
                            for field in form.fields:
                                form.fields[field].widget.attrs['disabled'] = True

        for s in remove_sections:
            sections.remove(s)

        context = {
            'CREATE_MODE': self.CREATE_MODE,
            'old_style_demographics': self.registry.code != 'fkrp',
            'current_registry_name': self.registry.name,
            'current_form_name': self.registry_form.display_name if self.registry_form.display_name else de_camelcase(self.registry_form.name),
            'registry': self.registry.code,
            'registry_code': self.registry.code,
            'form_name': self.form_id,
            'form_display_name': self.registry_form.name,
            'patient_id': self._get_patient_id(),
            'patient_link': PatientLocator(self.registry, patient_model).link,
            'patient_name': self._get_patient_name(),
            'sections': sections,
            'forms': form_section,
            'display_names': display_names,
            'section_ids': ids,
            "not_linked": patient_model.is_linked if patient_model else True,
            'section_element_map': section_element_map,
            "total_forms_ids": total_forms_ids,
            'section_field_ids_map': section_field_ids_map,
            "initial_forms_ids": initial_forms_ids,
            "formset_prefixes": formset_prefixes,
            "form_links": form_links,
            "metadata_json_for_sections": self._get_metadata_json_dict(self.registry_form),
            "has_form_progress": self.registry_form.has_progress_indicator,
            "have_dynamic_data": bool(self.dynamic_data),
            "settings": settings,
            "has_previous_data": self.has_previous_contexts,
            "previous_versions": self.previous_versions,
            "changes_since_version": changes_since_version,
        }

        if not self.registry_form.is_questionnaire and self.registry_form.has_progress_indicator:

            form_progress = FormProgress(self.registry_form.registry)

            form_progress_percentage = form_progress.get_form_progress(self.registry_form,
                                                                       patient_model,
                                                                       self.rdrf_context)

            form_cdes_status = form_progress.get_form_cdes_status(self.registry_form,
                                                                  patient_model,
                                                                  self.rdrf_context)

            context["form_progress"] = form_progress_percentage
            context["form_progress_cdes"] = form_cdes_status

        context.update(kwargs)
        return context

    def _get_patient_id(self):
        return self.patient_id

    def _get_patient_name(self):
        patient = Patient.objects.get(pk=self.patient_id)
        patient_name = '%s %s' % (patient.given_names, patient.family_name)
        return patient_name

    def _get_patient_object(self):
        return Patient.objects.get(pk=self.patient_id)

    def _get_metadata_json_dict(self, registry_form):
        """
        :param registry_form model instance
        :return: a dictionary of section --> metadata json for cdes in the section
        Used by the dynamic formset plugin client side to override behaviour

        We only provide overrides here at the moment
        """
        json_dict = {}
        from rdrf.helpers.utils import id_on_page
        for section in registry_form.get_sections():
            metadata = {}
            section_model = Section.objects.filter(code=section).first()
            if section_model:
                for cde_code in section_model.get_elements():
                    cde = CommonDataElement.objects.filter(code=cde_code).first()
                    if cde:
                        cde_code_on_page = id_on_page(registry_form, section_model, cde)
                        if cde.datatype.lower() == CDEDataTypes.DATE:
                            # date widgets are complex
                            metadata[cde_code_on_page] = {}
                            metadata[cde_code_on_page]["row_selector"] = cde_code_on_page + "_month"

            if metadata:
                json_dict[section] = json.dumps(metadata)

        return json_dict

    # fixme: could replace with TemplateView.get_template_names()
    def _get_template(self):
        if self.user and self.user.has_perm(
            "rdrf.form_%s_is_readonly" %
                self.form_id) and not self.user.is_superuser:
            return "rdrf_cdes/form_readonly.html"

        return "rdrf_cdes/form.html"


class FormPrintView(FormView):

    def _get_template(self):
        return "rdrf_cdes/form_print.html"


class FormListView(TemplateView):
    template_name = "rdrf_cdes/form_list.html"

    def get(self, request, **kwargs):
        patient_model = get_object_or_permission_denied(Patient, pk=kwargs.get('patient_id'))
        security_check_user_patient(request.user, patient_model)
        self.user = request.user

        return super().get(request, **kwargs)

    def _get_form_links(self, registry_code, form_id, patient_id):
        registry = get_object_or_404(Registry, code=registry_code)
        cfg = get_object_or_404(ContextFormGroup, pk=form_id, registry=registry)
        patient = get_object_or_404(Patient, pk=patient_id)

        return cfg.direct_name, [
            {
                "url": url,
                "text": text or _("Not set"),
            } for context_id, url, text in patient.get_forms_by_group(cfg, self.user)
        ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        form_name, form_links = self._get_form_links(**kwargs)
        context["form_title"] = form_name
        context["form_links"] = json.dumps(form_links)
        context["no_form_links"] = len(form_links) == 0
        return context


class FormFieldHistoryView(TemplateView):
    template_name = "rdrf_cdes/form_field_history.html"

    def get(self, request, **kwargs):
        if request.user.is_working_group_staff:
            raise PermissionDenied()
        patient_model = get_object_or_permission_denied(Patient, pk=kwargs.get('patient_id'))
        security_check_user_patient(request.user, patient_model)
        return super(FormFieldHistoryView, self).get(request, **kwargs)

    def get_context_data(
            self,
            registry_code,
            form_id,
            patient_id,
            context_id,
            section_code,
            cde_code,
            formset_index=None):
        context = super(FormFieldHistoryView, self).get_context_data()

        # find database objects from url route params
        reg = get_object_or_404(Registry, code=registry_code)
        reg_form = get_object_or_404(RegistryForm, registry=reg, pk=form_id)
        cde = get_object_or_404(CommonDataElement, code=cde_code)
        patient = get_object_or_404(Patient, pk=patient_id)
        rdrf_context = get_object_or_404(RDRFContext, registry=reg, pk=context_id)

        # grab snapshot values out of mongo documents
        dyn_patient = DynamicDataWrapper(patient, rdrf_context_id=rdrf_context.id)
        history = dyn_patient.get_cde_history(registry_code, reg_form.name,
                                              section_code, cde_code, formset_index)

        context.update({
            "cde": cde,
            "history": history,
        })
        return context


class ConsentFormWrapper(object):

    def __init__(self, label, form, consent_section_model):
        self.label = label
        self.form = form
        self.consent_section_model = consent_section_model  # handly

    def is_valid(self):
        return self.form.is_valid()

    @property
    def errors(self):
        messages = []
        for field in self.form.errors:
            for message in self.form.errors[field]:
                messages.append(_("Consent Section Invalid"))

        return messages

    @property
    def num_errors(self):
        return len(self.errors)


class ConsentQuestionWrapper(object):

    def __init__(self):
        self.label = ""
        self.answer = "No"


class QuestionnaireView(FormView):

    def __init__(self, *args, **kwargs):
        super(QuestionnaireView, self).__init__(*args, **kwargs)
        self.questionnaire_context = None
        self.template = 'rdrf_cdes/questionnaire.html'
        self.CREATE_MODE = False
        self.show_multisection_delete_checkbox = False
        self.init_previous_data_members()

    @method_decorator(patient_questionnaire_access)
    def get(self, request, registry_code, questionnaire_context="au"):
        try:
            if questionnaire_context is not None:
                self.questionnaire_context = questionnaire_context
            else:
                self.questionnaire_context = 'au'
            self.registry = self._get_registry(registry_code)
            form = self.registry.questionnaire
            if form is None:
                raise RegistryForm.DoesNotExist()

            self.registry_form = form
            context = self._build_context(questionnaire_context=questionnaire_context)

            custom_consent_helper = CustomConsentHelper(self.registry)
            custom_consent_helper.create_custom_consent_wrappers()

            context["custom_consent_errors"] = {}
            context["custom_consent_wrappers"] = custom_consent_helper.custom_consent_wrappers
            context["registry"] = self.registry
            context["country_code"] = questionnaire_context
            context["prelude_file"] = self._get_prelude(registry_code, questionnaire_context)
            context["show_print_button"] = False

            return self._render_context(request, context)
        except RegistryForm.DoesNotExist:
            context = {
                'registry': self.registry,
                'error_msg': 'No questionnaire for registry %s' % registry_code
            }
        except RegistryForm.MultipleObjectsReturned:
            context = {
                'registry': self.registry,
                'error_msg': "Multiple questionnaire exists for %s" % registry_code
            }
        return render(request, 'rdrf_cdes/questionnaire_error.html', context)

    def _get_template(self):
        return "rdrf_cdes/questionnaire.html"

    def _get_prelude(self, registry_code, questionnaire_context):
        if questionnaire_context is None:
            prelude_file = "prelude_%s.html" % registry_code
        else:
            prelude_file = "prelude_%s_%s.html" % (registry_code, questionnaire_context)

        file_path = os.path.join(settings.TEMPLATES[0]["DIRS"][0], 'rdrf_cdes', prelude_file)
        if os.path.exists(file_path):
            return os.path.join('rdrf_cdes', prelude_file)
        else:
            return None

    def _get_questionnaire_context(self, request):
        parts = request.path.split("/")
        context_flag = parts[-1]
        if context_flag in ["au", "nz"]:
            return context_flag
        else:
            return "au"

    def _create_custom_consents_wrappers(self, post_data=None):
        consent_form_wrappers = []

        for consent_section_model in self.registry.consent_sections.order_by("code"):
            consent_form_class = create_form_class_for_consent_section(
                self.registry, consent_section_model)
            consent_form = consent_form_class(data=post_data)
            consent_form_wrapper = ConsentFormWrapper(
                consent_section_model.section_label, consent_form, consent_section_model)
            consent_form_wrappers.append(consent_form_wrapper)

        return consent_form_wrappers

    @method_decorator(patient_questionnaire_access)
    def post(self, request, registry_code, **kwargs):
        error_count = 0
        registry = self._get_registry(registry_code)
        self.registry = registry

        # RDR-871 Allow "custom consents"
        custom_consent_helper = CustomConsentHelper(self.registry)
        custom_consent_helper.get_custom_consent_keys_from_request(request)
        custom_consent_helper.create_custom_consent_wrappers()
        custom_consent_helper.check_for_errors()

        error_count += custom_consent_helper.error_count

        self.questionnaire_context = self._get_questionnaire_context(request)

        questionnaire_form = registry.questionnaire
        self.registry_form = questionnaire_form
        sections, display_names, ids = self._get_sections(registry.questionnaire)
        # section --> dynamic data for questionnaire response object if no errors
        data_map = {}
        # section --> form instances if there are errors and form needs to be redisplayed
        form_section = {}
        formset_prefixes = {}
        total_forms_ids = {}
        initial_forms_ids = {}
        section_element_map = {}
        # this is used by formset plugin:
        # the full ids on form eg { "section23": ["form23^^sec01^^CDEName", ... ] , ...}
        section_field_ids_map = {}

        for section in sections:
            section_model = Section.objects.get(code=section)
            section_elements = section_model.get_elements()
            section_element_map[section] = section_elements
            form_class = create_form_class_for_section(
                registry,
                questionnaire_form,
                section_model,
                questionnaire_context=self.questionnaire_context)
            section_field_ids_map[section] = self._get_field_ids(form_class)

            if not section_model.allow_multiple:
                form = form_class(request.POST, request.FILES)
                form_section[section] = form
                if form.is_valid():
                    dynamic_data = form.cleaned_data
                    data_map[section] = dynamic_data
                else:
                    for e in form.errors:
                        error_count += 1
            else:
                if section_model.extra:
                    extra = section_model.extra
                else:
                    extra = 0

                prefix = "formset_%s" % section
                form_set_class = formset_factory(form_class, extra=extra, can_delete=True)
                form_section[section] = form_set_class(
                    request.POST, request.FILES, prefix=prefix)
                formset_prefixes[section] = prefix
                total_forms_ids[section] = "id_%s-TOTAL_FORMS" % prefix
                initial_forms_ids[section] = "id_%s-INITIAL_FORMS" % prefix
                formset = form_set_class(request.POST, prefix=prefix)

                if formset.is_valid():
                    dynamic_data = formset.cleaned_data  # a list of values
                    section_dict = {}
                    section_dict[section] = dynamic_data
                    data_map[section] = section_dict
                else:
                    for e in formset.errors:
                        error_count += 1

        if error_count == 0:
            questionnaire_response = QuestionnaireResponse()
            questionnaire_response.registry = registry
            questionnaire_response.save()
            questionnaire_response_wrapper = DynamicDataWrapper(questionnaire_response)
            questionnaire_response_wrapper.current_form_model = questionnaire_form
            questionnaire_response_wrapper.save_dynamic_data(
                registry_code, "cdes", {
                    "custom_consent_data": custom_consent_helper.custom_consent_data})

            for section in sections:
                data_map[section]['questionnaire_context'] = self.questionnaire_context
                if is_multisection(section):
                    questionnaire_response_wrapper.save_dynamic_data(
                        registry_code, "cdes", data_map[section], multisection=True,
                        additional_data={"questionnaire_context": self.questionnaire_context})
                else:
                    questionnaire_response_wrapper.save_dynamic_data(
                        registry_code, "cdes", data_map[section],
                        additional_data={"questionnaire_context": self.questionnaire_context})

            def get_completed_questions(
                    questionnaire_form_model,
                    data_map,
                    custom_consent_data,
                    consent_wrappers):
                section_map = OrderedDict()

                class SectionWrapper(object):

                    def __init__(self, label):
                        self.label = label
                        self.is_multi = False
                        self.subsections = []
                        self.questions = []

                    def load_consents(self, consent_section_model, custom_consent_data):
                        for consent_question_model in consent_section_model.questions.order_by(
                                "position"):
                            question_wrapper = ConsentQuestionWrapper()
                            question_wrapper.label = consent_question_model.label(
                                on_questionnaire=True)
                            field_key = consent_question_model.field_key
                            try:
                                value = custom_consent_data[field_key]
                                if value == "on":
                                    question_wrapper.answer = "Yes"
                            except KeyError:
                                pass
                            self.questions.append(question_wrapper)

                class Question(object):

                    def __init__(self, delimited_key, value):
                        self.delimited_key = delimited_key              # in Mongo
                        self.value = value                              # value in Mongo
                        # label looked up via cde code
                        self.label = self._get_label(delimited_key)
                        # display value if a range
                        self.answer = self._get_answer()
                        self.is_multi = False

                    def _get_label(self, delimited_key):
                        _, _, cde_code = delimited_key.split("____")
                        cde_model = CommonDataElement.objects.get(code=cde_code)
                        return cde_model.name

                    def _get_answer(self):
                        if self.value is None:
                            return " "
                        elif self.value == "":
                            return " "
                        else:
                            cde_model = self._get_cde_model()
                            if cde_model.pv_group:
                                range_dict = cde_model.pv_group.as_dict
                                for value_dict in range_dict["values"]:
                                    if value_dict["code"] == self.value:
                                        if value_dict["questionnaire_value"]:
                                            return value_dict["questionnaire_value"]
                                        else:
                                            return value_dict["value"]
                            elif cde_model.datatype == 'boolean':
                                if self.value:
                                    return "Yes"
                                else:
                                    return "No"
                            elif cde_model.datatype == 'date':
                                return parse_iso_date(self.value).strftime("%d-%m-%Y")
                            return str(self.value)

                    def _get_cde_model(self):
                        _, _, cde_code = self.delimited_key.split("____")
                        return CommonDataElement.objects.get(code=cde_code)

                def get_question(form_model, section_model, cde_model, data_map):
                    from rdrf.helpers.utils import mongo_key_from_models
                    delimited_key = mongo_key_from_models(form_model, section_model, cde_model)
                    section_data = data_map[section_model.code]
                    if delimited_key in section_data:
                        value = section_data[delimited_key]
                    else:
                        value = None
                    return Question(delimited_key, value)

                # Add custom consents first so they appear on top
                for consent_wrapper in custom_consent_helper.custom_consent_wrappers:
                    sw = SectionWrapper(consent_wrapper.label)
                    consent_section_model = consent_wrapper.consent_section_model
                    sw.load_consents(
                        consent_section_model, custom_consent_helper.custom_consent_data)
                    section_map[sw.label] = sw

                for section_model in questionnaire_form_model.section_models:
                    section_label = section_model.questionnaire_display_name or section_model.display_name
                    if section_label not in section_map:
                        section_map[section_label] = SectionWrapper(section_label)
                    if not section_model.allow_multiple:

                        for cde_model in section_model.cde_models:
                            question = get_question(
                                questionnaire_form_model, section_model, cde_model, data_map)
                            section_map[section_label].questions.append(question)
                    else:
                        section_map[section_label].is_multi = True

                        for multisection_map in data_map[
                                section_model.code][
                                section_model.code]:
                            subsection = []
                            section_wrapper = {section_model.code: multisection_map}
                            for cde_model in section_model.cde_models:
                                question = get_question(
                                    questionnaire_form_model,
                                    section_model,
                                    cde_model,
                                    section_wrapper)
                                subsection.append(question)
                            section_map[section_label].subsections.append(subsection)

                return section_map

            section_map = get_completed_questions(questionnaire_form,
                                                  data_map,
                                                  custom_consent_helper.custom_consent_data,
                                                  custom_consent_helper.custom_consent_wrappers)

            context = {}
            context["custom_consent_errors"] = {}
            context["completed_sections"] = section_map
            context["prelude"] = self._get_prelude(registry_code, self.questionnaire_context)

            return render(request, 'rdrf_cdes/completed_questionnaire_thankyou.html', context)
        else:
            context = {
                'custom_consent_wrappers': custom_consent_helper.custom_consent_wrappers,
                'custom_consent_errors': custom_consent_helper.custom_consent_errors,
                'registry': registry_code,
                'form_name': 'questionnaire',
                'form_display_name': registry.questionnaire.name,
                'patient_id': 'dummy',
                'patient_name': '',
                'sections': sections,
                'forms': form_section,
                'display_names': display_names,
                'section_element_map': section_element_map,
                'section_field_ids_map': section_field_ids_map,
                "total_forms_ids": total_forms_ids,
                "initial_forms_ids": initial_forms_ids,
                "formset_prefixes": formset_prefixes,
                "metadata_json_for_sections": self._get_metadata_json_dict(self.registry_form)
            }

            context.update(csrf(request))
            messages.add_message(request, messages.ERROR, _(
                'The questionnaire was not submitted because of validation errors - please try again'))
            return render(request, 'rdrf_cdes/questionnaire.html', context)

    def _get_patient_id(self):
        return "questionnaire"

    def _get_patient_name(self):
        return "questionnaire"

    def _get_form_class_for_section(self, registry, registry_form, section, allowed_cdes, previous_values):
        return create_form_class_for_section(
            registry, registry_form, section,
            allowed_cdes=allowed_cdes,
            previous_values=previous_values,
            questionnaire_context=self.questionnaire_context
        )


class QuestionnaireHandlingView(StaffMemberRequiredMixin, View):

    @csp_update(SCRIPT_SRC=["'unsafe-eval'"])
    def get(self, request, registry_code, questionnaire_response_id):
        from rdrf.workflows.questionnaires.questionnaires import Questionnaire
        context = csrf(request)
        template_name = "rdrf_cdes/questionnaire_handling.html"
        context["registry_model"] = get_object_or_404(Registry, code=registry_code)
        context["form_model"] = context["registry_model"].questionnaire
        context["qr_model"] = get_object_or_404(
            QuestionnaireResponse, id=questionnaire_response_id)
        context["patient_lookup_url"] = reverse("patient_lookup", args=(registry_code,))

        context["questionnaire"] = Questionnaire(context["registry_model"],
                                                 context["qr_model"])

        return render(request, template_name, context)


class FileUploadView(FileErrorHandlingMixin, View):

    def get(self, request, registry_code, file_id):
        file_info = filestorage.get_file(file_id)
        if file_info.patient:
            security_check_user_patient(request.user, file_info.patient)
        else:
            raise PermissionDenied
        check_status = request.GET.get('check_status', '')
        need_status_check = check_status and check_status.lower() == 'true'
        if need_status_check:
            cde_file = get_object_or_404(CDEFile, pk=file_id)
            return JsonResponse({
                "response": virus_checker_result(cde_file.item.name),
            })

        if file_info.item is not None:
            response = FileResponse(file_info.item, content_type=file_info.mime_type or 'application/octet-stream')
            response['Content-disposition'] = 'filename="%s"' % file_info.filename
            return response
        return HttpResponseNotFound()


class StandardView(object):
    TEMPLATE_DIR = 'rdrf_cdes'
    INFORMATION = "information.html"
    APPLICATION_ERROR = "application_error.html"

    @staticmethod
    def _render(request, view_type, context):
        context.update(csrf(request))
        template = StandardView.TEMPLATE_DIR + "/" + view_type
        return render(request, template, context)

    @staticmethod
    def render_information(request, message):
        context = {"message": message}
        return StandardView._render(request, StandardView.INFORMATION, context)

    @staticmethod
    def render_error(request, error_message):
        context = {"application_error": error_message}
        return StandardView._render(request, StandardView.APPLICATION_ERROR, context)


class QuestionnaireConfigurationView(StaffMemberRequiredMixin, View):

    """
    Allow an admin to choose which fields to expose in the questionnaire for a given cinical form
    """
    TEMPLATE = "rdrf_cdes/questionnaire_config.html"

    def get(self, request, form_pk):
        registry_form = RegistryForm.objects.get(pk=form_pk)

        class QuestionWrapper(object):

            def __init__(self, registry_form, section_model, cde_model):
                self.registry_form = registry_form
                self.section_model = section_model
                self.cde_model = cde_model

            @property
            def clinical(self):
                return self.cde_model.name  # The clinical label

            @property
            def questionnaire(self):
                # The text configured at the cde level for the questionnaire
                return self.cde_model.questionnaire_text

            @property
            def section(self):
                return self.section_model.display_name

            @property
            def code(self):
                return self.section_model.code + "." + self.cde_model.code

            @property
            def exposed(self):
                if self.registry_form.on_questionnaire(
                        self.section_model.code,
                        self.cde_model.code):
                    return "checked"
                else:
                    return ""

        sections = []

        class SectionWrapper(object):

            def __init__(self, registry_form, section_model):
                self.registry_form = registry_form
                self.section_model = section_model

            @property
            def questions(self):
                lst = []
                for cde_model in self.section_model.cde_models:
                    lst.append(QuestionWrapper(self.registry_form, self.section_model, cde_model))
                return lst

            @property
            def name(self):
                return self.section_model.display_name

        for section_model in registry_form.section_models:
            sections.append(SectionWrapper(registry_form, section_model))

        context = {"registry_form": registry_form, "sections": sections}
        return self._render_context(request, context)

    def _render_context(self, request, context):
        context.update(csrf(request))
        return render(request, self.TEMPLATE, context)


class RPCHandler(View):

    def post(self, request):
        action_dict = json.loads(request.body.decode("utf-8"))
        action_executor = ActionExecutor(request, action_dict)
        client_response_dict = action_executor.run()
        client_response_json = json.dumps(client_response_dict)
        return HttpResponse(client_response_json, status=200, content_type="application/json")


class Colours(object):
    grey = "#808080"
    blue = "#0000ff"
    green = "#00ff00"
    red = "#f7464a"
    yellow = "#ffff00"


class CustomConsentFormView(View):

    def get(self, request, registry_code, patient_id, context_id=None):
        if not request.user.is_authenticated:
            consent_form_url = reverse('consent_form_view', args=[registry_code, patient_id])
            login_url = reverse('two_factor:login')
            return redirect("%s?next=%s" % (login_url, consent_form_url))

        patient_model = get_object_or_permission_denied(Patient, pk=patient_id)

        security_check_user_patient(request.user, patient_model)

        registry_model = Registry.objects.get(code=registry_code)
        form_sections = self._get_form_sections(request.user, registry_model, patient_model)
        wizard = NavigationWizard(request.user,
                                  registry_model,
                                  patient_model,
                                  NavigationFormType.CONSENTS,
                                  context_id,
                                  None)

        try:
            parent = ParentGuardian.objects.get(user=request.user)
        except ParentGuardian.DoesNotExist:
            parent = None

        context_launcher = RDRFContextLauncherComponent(request.user,
                                                        registry_model,
                                                        patient_model,
                                                        current_form_name="Consents")

        patient_info = RDRFPatientInfoComponent(registry_model, patient_model, request.user)

        context = {
            "location": "Consents",
            "forms": form_sections,
            "context_id": context_id,
            "show_archive_button": request.user.can_archive,
            "not_linked": not patient_model.is_linked,
            "archive_patient_url": patient_model.get_archive_url(registry_model) if request.user.can_archive else "",
            "form_name": "fixme",  # required for form_print link
            "patient": patient_model,
            "patient_id": patient_model.id,
            "registry_code": registry_code,
            'patient_link': PatientLocator(registry_model, patient_model).link,
            "context_launcher": context_launcher.html,
            "patient_info": patient_info.html,
            "next_form_link": wizard.next_link,
            "previous_form_link": wizard.previous_link,
            "parent": parent,
            "consent": consent_status_for_patient(registry_code, patient_model),
            "show_print_button": True,
            "can_sign_consent": can_sign_consent(request.user, patient_model)
        }

        return render(request, "rdrf_cdes/custom_consent_form.html", context)

    def _get_initial_consent_data(self, patient_model):
        # load initial consent data for custom consent form
        if patient_model is None:
            return {}
        initial_data = {}
        data = patient_model.consent_questions_data
        for consent_field_key in data:
            initial_data[consent_field_key] = data[consent_field_key]
        return initial_data

    def _get_form_sections(self, request_user, registry_model, patient_model):
        custom_consent_form_generator = CustomConsentFormGenerator(
            registry_model, patient_model, request_user)
        initial_data = self._get_initial_consent_data(patient_model)
        custom_consent_form = custom_consent_form_generator.create_form(initial_data)

        patient_consent_file_forms = self._get_consent_file_formset(patient_model)

        consent_sections = custom_consent_form.get_consent_sections()

        patient_section_consent_file = (_(render_to_string("rdrf_cdes/consent_instructions.html")), None)

        patient_signature_form = None
        patient_signature = None
        consent_config = getattr(registry_model, 'consent_configuration', None)
        signature_supported = consent_config and (consent_config.signature_required or consent_config.signature_enabled)
        if consent_sections and signature_supported:
            patient_signature = (_("Patient signature"), ["signature"])
            signature = PatientSignature.objects.filter(patient=patient_model).first()
            patient_signature_form = PatientSignatureForm(
                data=None, prefix="patient_signature", instance=signature, registry_model=registry_model,
                can_sign_consent=can_sign_consent(request_user, patient_model)
            )

        return self._section_structure(custom_consent_form,
                                       consent_sections,
                                       patient_consent_file_forms,
                                       patient_section_consent_file,
                                       patient_signature_form,
                                       patient_signature)

    def _get_consent_file_formset(self, patient_model):
        patient_consent_file_formset = inlineformset_factory(
            Patient,
            PatientConsent,
            form=PatientConsentFileForm,
            extra=0,
            can_delete=True,
            fields="__all__")

        patient_consent_file_forms = patient_consent_file_formset(instance=patient_model,
                                                                  prefix="patient_consent_file")
        return patient_consent_file_forms

    def _section_structure(self,
                           custom_consent_form,
                           consent_sections,
                           patient_consent_file_forms,
                           patient_section_consent_file,
                           patient_signature_form=None,
                           patient_signature=None):
        structure = [
            (
                custom_consent_form,
                consent_sections,
            ),
            (
                patient_consent_file_forms,
                (patient_section_consent_file,),
            ),
        ]
        if patient_signature_form and patient_signature:
            structure.append((patient_signature_form, (patient_signature,),))
        return structure

    def _get_success_url(self, registry_model, patient_model):
        return reverse("consent_form_view", args=[registry_model.code,
                                                  patient_model.pk])

    def post(self, request, registry_code, patient_id, context_id=None):
        if not request.user.is_authenticated:
            consent_form_url = reverse(
                'consent_form_view', args=[
                    registry_code, patient_id, context_id])
            login_url = reverse('two_factor:login')
            return redirect("%s?next=%s" % (login_url, consent_form_url))

        registry_model = Registry.objects.get(code=registry_code)
        patient_model = get_object_or_permission_denied(Patient, pk=patient_id)
        security_check_user_patient(request.user, patient_model)

        context_launcher = RDRFContextLauncherComponent(request.user,
                                                        registry_model,
                                                        patient_model,
                                                        current_form_name="Consents")

        wizard = NavigationWizard(request.user,
                                  registry_model,
                                  patient_model,
                                  NavigationFormType.CONSENTS,
                                  context_id,
                                  None)

        patient_consent_file_formset = inlineformset_factory(Patient, PatientConsent,
                                                             form=PatientConsentFileForm,
                                                             fields="__all__")

        patient_consent_file_forms = patient_consent_file_formset(request.POST,
                                                                  request.FILES,
                                                                  instance=patient_model,
                                                                  prefix="patient_consent_file")
        for f in patient_consent_file_forms:
            if f.instance:
                security_check_user_patient(request.user, f.instance.patient)

        patient_section_consent_file = (_(render_to_string("rdrf_cdes/consent_instructions.html")), None)

        patient_signature = (_("Patient signature"), ["signature"])

        custom_consent_form_generator = CustomConsentFormGenerator(
            registry_model, patient_model, request.user)
        custom_consent_form = custom_consent_form_generator.create_form(request.POST)
        consent_sections = custom_consent_form.get_consent_sections()

        forms_to_validate = [custom_consent_form, patient_consent_file_forms]

        patient_signature_form = None
        consent_config = getattr(registry_model, 'consent_configuration', None)
        signature_supported = consent_config and (consent_config.signature_required or consent_config.signature_enabled)
        if consent_sections and signature_supported:
            signature, __ = PatientSignature.objects.get_or_create(patient=patient_model)
            patient_signature_form = PatientSignatureForm(
                request.POST, prefix="patient_signature", instance=signature, registry_model=registry_model,
                can_sign_consent=can_sign_consent(request.user, patient_model)
            )
            patient_signature_form.patient = patient_model
            forms_to_validate.append(patient_signature_form)

            form_sections = self._section_structure(
                custom_consent_form, consent_sections,
                patient_consent_file_forms, patient_section_consent_file,
                patient_signature_form, patient_signature
            )
        else:
            form_sections = self._section_structure(
                custom_consent_form, consent_sections,
                patient_consent_file_forms, patient_section_consent_file
            )

        valid_forms = []
        error_messages = []

        for form in forms_to_validate:
            if not form.is_valid():
                valid_forms.append(False)
                if isinstance(form.errors, list):
                    for error_dict in form.errors:
                        for field, values in error_dict.items():
                            for value in values:
                                error_messages.append("%s: %s" % (field, value))
                else:
                    for field in form.errors:
                        for error in form.errors[field]:
                            error_messages.append(error)
            else:
                valid_forms.append(True)

        if all(valid_forms):
            patient_consent_file_forms.save()
            custom_consent_form.save()
            if patient_signature_form:
                patient_signature_form.save()
            get_registry_stage_flow(registry_model).handle(patient_model)
            patient_name = "%s %s" % (patient_model.given_names, patient_model.family_name)
            messages.success(
                self.request,
                _("Patient %(patient_name)s saved successfully. Please now use the blue arrow on the right to continue.") % {
                    "patient_name": patient_name})
            return HttpResponseRedirect(self._get_success_url(registry_model, patient_model))
        else:
            try:
                parent = ParentGuardian.objects.get(user=request.user)
            except ParentGuardian.DoesNotExist:
                parent = None

            context = dict({
                "location": "Consents",
                "patient": patient_model,
                "patient_id": patient_model.id,
                'patient_link': PatientLocator(
                    registry_model,
                    patient_model).link,
                "context_id": context_id,
                "registry_code": registry_code,
                "show_archive_button": request.user.can_archive,
                "not_linked": not patient_model.is_linked,
                "archive_patient_url": patient_model.get_archive_url(registry_model) if request.user.can_archive else "",
                "next_form_link": wizard.next_link,
                "previous_form_link": wizard.previous_link,
                "context_launcher": context_launcher.html,
                "forms": form_sections,
                "error_messages": [],
                "parent": parent,
                "consent": consent_status_for_patient(registry_code, patient_model),
                "can_sign_consent": can_sign_consent(request.user, patient_model),
            })

            context["message"] = _("Consent section not complete")
            context["error_messages"] = error_messages
            context["errors"] = True

            return render(request, "rdrf_cdes/custom_consent_form.html", context)


class FormDSLHelpView(TemplateView):
    template_name = "rdrf_cdes/form-dsl-help.html"


class CdeWidgetSettingsView(View):

    def get(self, request, code, new_name):
        cde = CommonDataElement.objects.filter(code=code).first() or CommonDataElement()
        cde.widget_name = new_name
        admin_form = CommonDataElementAdminForm(cde.__dict__, instance=cde)
        is_hidden = admin_form['widget_settings'].is_hidden
        display = 'style="display:none"' if is_hidden else ''
        hidden_input = '<input type="hidden" name="widget_settings" value="{}" id="id_widget_settings">'
        ret_val = """
            <div class="form-row field-widget_settings" {}>
              <div>
                <label class="{}" for="id_widget_settings">Widget settings:</label>
                {}
              </div>
            </div>
        """.format(display, 'hidden' if is_hidden else '', hidden_input if is_hidden else admin_form['widget_settings'].as_widget())
        return HttpResponse(mark_safe(ret_val))


class CdeAvailableWidgetsView(View):

    def get(self, request, data_type):
        widgets = [{'name': name, 'value': name} for name in sorted(get_widgets_for_data_type(data_type))]
        return JsonResponse({'widgets': widgets})
