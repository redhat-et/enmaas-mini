"""AWS CLI environment for real AWS deployment helpers, never custom endpoints."""
import os


def environment(profile=None):
    result=dict(os.environ)
    # Explicit profile selection must not accidentally inherit another principal.
    # With no profile, preserve the existing environment-only one-node workflow.
    if profile:
        for key in ('AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_SESSION_TOKEN','AWS_SECURITY_TOKEN',
                    'AWS_PROFILE','AWS_DEFAULT_PROFILE','AWS_ROLE_ARN','AWS_WEB_IDENTITY_TOKEN_FILE'):
            result.pop(key,None)
    for key in list(result):
        if key=='AWS_ENDPOINT_URL' or key.startswith('AWS_ENDPOINT_URL_'):
            result.pop(key)
    result.update(AWS_IGNORE_CONFIGURED_ENDPOINT_URLS='true',AWS_CLI_AUTO_PROMPT='off',
                  AWS_MAX_ATTEMPTS='1',AWS_RETRY_MODE='standard')
    return result
