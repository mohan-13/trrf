from rdrf.models.workflow_models import ClinicianSignupRequest


class RegistrationWorkflow:
    def __init__(self, token, request_object):
        self.token = token
        self.request_object = request_object

    def get_template(self):
        pass

    @property
    def username(self):
        return self.get_username()

    @property
    def first_name(self):
        return self.get_first_name()

    @property
    def last_name(self):
        return self.get_last_name()

    def get_username(self):
        return None

    def get_first_name(self):
        return None

    def get_last_name(self):
        return None


class ClinicianSignupWorkflow(RegistrationWorkflow):
    def get_template(self):
        return "registration/clinician_registration.html"

    def get_username(self):
        return self.request_object.clinician_email

    def get_first_name(self):
        return self.request_object.clinician_other.clinician_first_name

    def get_last_name(self):
        return self.request_object.clinician_other.clinician_last_name


class FormRegistrationWorkflow(RegistrationWorkflow):
    def get_template(self):
        return "registration/registration_form_simple.html"


class PatientRegistrationWorkflow(RegistrationWorkflow):
    def get_template(self):
        return "registration/registration_form_patient.html"


def get_registration_workflow(token):
    try:
        csr = ClinicianSignupRequest.objects.get(token=token,
                                                 state="emailed")
        return ClinicianSignupWorkflow(token, csr)
    except ClinicianSignupRequest.DoesNotExist:
        pass


def get_default_registration_workflow(user, request):
    from django.conf import settings

    if hasattr(settings, "REGISTRATION_CLASS"):
        from django.utils.module_loading import import_string
        registration_class = import_string(settings.REGISTRATION_CLASS)
        reg = registration_class(user, request)
        return reg.get_registration_workflow()
    else:
        return FormRegistrationWorkflow(None, None)
