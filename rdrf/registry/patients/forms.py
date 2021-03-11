from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import ugettext as _


class ParentAddPatientForm(forms.Form):
    family_name = forms.CharField(label=_("Surname"))
    given_names = forms.CharField(label=_("Given names"))
    date_of_birth = forms.DateField(label=_("Date of birth"))
    sex = forms.CharField(max_length=1, label=_("Sex"))
    use_parent_address = forms.BooleanField(label=_("Use parent address"))

    address = forms.CharField(required=False)
    suburb = forms.CharField(required=False)
    state = forms.CharField(required=False)
    postcode = forms.CharField(required=False)
    country = forms.CharField(required=False)

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data['use_parent_address']:
            address_fields = [cleaned_data['address'],
                              cleaned_data['suburb'],
                              cleaned_data['state'],
                              cleaned_data['postcode'],
                              cleaned_data['country']]
            for field in address_fields:
                if not field:
                    raise ValidationError(_("Missing address field"))
        return cleaned_data
