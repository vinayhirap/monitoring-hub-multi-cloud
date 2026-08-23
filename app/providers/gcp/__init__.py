# app/providers/gcp/__init__.py
from app.providers.registry import register
from app.providers.gcp.provider import GCPProvider

register("gcp", GCPProvider)
