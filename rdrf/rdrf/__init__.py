VERSION = "3.1.3"
__version__ = VERSION

# Ensures db router system check is registered
from . import db


default_app_config = 'rdrf.apps.RDRFConfig'
