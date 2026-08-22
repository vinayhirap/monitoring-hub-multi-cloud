from app.providers.registry import register
from app.providers.aws.provider import AWSProvider

register("aws", AWSProvider)
