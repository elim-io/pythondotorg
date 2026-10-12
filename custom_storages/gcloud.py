"""Google Cloud Storage backends for media and manifest-based static files on Cloud Run."""

from django.contrib.staticfiles.storage import ManifestFilesMixin
from pipeline.storage import PipelineMixin
from storages.backends.gcloud import GoogleCloudStorage

from custom_storages.storages import CommentAwareManifestMixin


class GoogleCloudPipelineManifestStorage(
    CommentAwareManifestMixin, PipelineMixin, ManifestFilesMixin, GoogleCloudStorage
):
    """Static files in a Cloud Storage bucket, with hashed names and the manifest beside them.

    ``collectstatic`` writes them, and the manifest (``staticfiles.json``) is read back from
    the bucket when the app starts.
    """
