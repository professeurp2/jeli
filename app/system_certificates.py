"""Trust the operating system's certificate store on developer machines (Windows, macOS).

Antivirus software and corporate proxies that inspect HTTPS (Avast Web Shield, for instance)
re-sign traffic with their own root certificate, installed in the system store. Python's bundled
certificates don't include it, so calls to Gemini fail with CERTIFICATE_VERIFY_FAILED on such
machines. Linux servers (Railway) keep Python's default behaviour.
"""

import sys


def use_system_certificates() -> None:
    if sys.platform in ("win32", "darwin"):
        import truststore

        truststore.inject_into_ssl()
