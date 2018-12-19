from django.urls import re_path
from rdrf.services.rest.views import proms_api

# app_name = 'rdrf'

urlpatterns = [
    re_path(r'surveys/?$', proms_api.SurveyEndpoint.as_view(), name='survey_endpoint'),
    re_path(r'surveyassignments/?$', proms_api.SurveyAssignments.as_view(), name='survey_assignments'),
    re_path(r'promsdownload/?$', proms_api.PromsDownload.as_view(), name='proms_download'),
]
