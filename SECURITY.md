# Security policy

This is an experimental fork with explicit network trust configuration. Do not
expose Companion or administration to the Internet, grant a Docker socket, or
use an authentication-bypassing Plex server configuration.

Security work is tracked in this fork. The original upstream does not maintain
these changes. Prefer a private GitHub vulnerability report if the repository
supports it; otherwise contact the repository owner privately before posting
credentials, network captures or a working exploit. Never include real Plex
tokens in a report.

Package findings require vendor/version and runtime applicability review. This
fork's source hardening does not automatically repair all Debian base packages.
