# Security policy

## Protecting credentials

Never commit or share real API keys, Meta access tokens, service-account JSON, webhook verification values, Cloudflare credentials, browser cookies, or private spreadsheet links. Keep real configuration in the ignored `.env` file and local credential files. Use `.env.example` only for placeholder configuration names.

Before pushing, review `git status`, confirm credential and media files are ignored, and scan the staged diff for secrets. Do not attach secret-bearing files to public issues.

## If a secret is exposed

Revoke or rotate the exposed credential with its provider, update the local `.env` or credential file, and check the provider's activity/usage page. Removing the value in a later commit does not remove it from Git history.

## Reporting a security issue

Do not publish working credentials or exploitable details in a public issue. If private vulnerability reporting is enabled for the repository, use that channel; otherwise contact the repository owner privately.
