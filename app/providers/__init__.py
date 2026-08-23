"""
Importing this package registers all built-in cloud providers with the
registry. Call app.providers.registry.get_provider("aws") after this
import to get a working AWSProvider instance.
"""
from app.providers import aws    # noqa: F401  (import registers AWSProvider)
from app.providers import azure  # noqa: F401  (import registers AzureProvider)
from app.providers import gcp    # noqa: F401  (import registers GCPProvider)
