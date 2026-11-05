# Security Policy

PICO handles private context, credentials, local files, browser sessions, and optional smart-home integrations. Security reports should be shared privately so that maintainers can investigate without exposing users or systems.

## Supported Versions

PICO does not yet publish a formal version-support matrix. Until one is published, please report vulnerabilities that affect the latest `main` branch. Historical commits and long-lived feature branches are not guaranteed to receive security fixes.

## Reporting a Vulnerability

**Please do not disclose suspected vulnerabilities in public issues, pull requests, discussions, or social posts.**

1. Open the repository's [Security and quality page](https://github.com/Lurkaee/PICO/security).
2. When **Report a vulnerability** is available, use GitHub's private vulnerability reporting form.
3. If private vulnerability reporting is not enabled, open a minimal public issue asking for a private security contact. Do not include exploit details, affected private data, credentials, or proof-of-concept code in that issue.

Private vulnerability reporting is a repository setting that must be enabled by an owner or administrator. The presence of this policy file does not itself enable that GitHub feature.

## What to Include

A useful report may include:

- The affected commit SHA, release/tag (if applicable), and subsystem.
- A concise description of the security impact and realistic attack scenario.
- Minimal reproduction steps or a proof of concept that does not affect systems or data you do not own or have explicit permission to test.
- Relevant environment details and sanitized logs.
- Any practical mitigations you have identified.

Redact API keys, tokens, passwords, private paths, conversation content, household details, device identifiers, and other personal or sensitive information.

## Research and Testing Boundaries

Please test only in an isolated environment that you own or are explicitly authorized to assess. Do not:

- Access, alter, delete, or exfiltrate another person's data.
- Attempt denial-of-service, destructive actions, persistence, or credential harvesting.
- Trigger real filesystem changes, external communications, or physical smart-home actuation on systems that are not yours or for which you lack explicit authorization.
- Attempt to bypass safeguards against live services or devices as part of testing against a third party.
- Publish secrets, real household telemetry, or unredacted logs.

Use mocks and synthetic fixtures wherever possible. A perception event, model proposal, or untrusted external document must never be treated as authorization to execute an action.

## Disclosure and Response

The maintainer will review reports and coordinate a reasonable remediation and disclosure approach with the reporter. There is currently no guaranteed response or remediation SLA and no bug-bounty program is being offered.

Please allow reasonable time for investigation and a fix before public disclosure. The maintainer may ask for clarification or a safer reproduction method. A report is not a guarantee that a particular severity, fix, or release timeline will be accepted.
