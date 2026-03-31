"""
Google Secret Manager helper.
Shared across all pipelines for credential retrieval.
"""

from google.cloud import secretmanager

_client = None


def get_secret(project_id: str, secret_id: str) -> str:
    """Retrieve the latest version of a secret from Secret Manager."""
    global _client
    if _client is None:
        _client = secretmanager.SecretManagerServiceClient()

    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    response = _client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8")
