# app/providers/azure/__init__.py
from app.providers.registry import register
from app.providers.azure.provider import AzureProvider

register("azure", AzureProvider)
