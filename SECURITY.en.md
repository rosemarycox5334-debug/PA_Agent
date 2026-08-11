# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest `main` branch | Yes |
| Old tags / branches | Reference only; fixes not guaranteed |

## Reporting Vulnerabilities or Key Leaks

**Do NOT paste API Keys, encrypted `api_key_encrypted`, complete `settings.json`, or analysis records containing personal account information in public Issues.**

Please contact the maintainers privately via:

- GitHub: **Security Advisories** (Repository → Security → Report a vulnerability), or
- QQ Group (see README) private message to maintainers

Please include the following in your report:

- Issue type (accidentally committed key, local file permissions, dependency vulnerability, etc.)
- Impact scope and reproduction steps
- Whether the key has been exposed in public repository history (if so, please indicate approximate time to assist with rotation)

## User Self-Checklist

1. Confirm `config/settings.json` has not been `git add`-ed (it should be ignored by `.gitignore`).
2. Run `tools\setup_git_secrets.ps1` to enable pre-commit interception.
3. If the key entered Git history: rotate the key at your provider and clean Git history or obsolete repository mirrors.
4. When open-sourcing a fork, delete private data in `records/`, `logs/`, `experience/` before pushing.

## Disclaimer

This software is a trading analysis auxiliary tool and does not provide hosting services; security configuration (API, MT5 accounts) is the sole responsibility of the user.
