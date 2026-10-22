"""Security, origin allowlisting, and SSRF validation for browser automation (Milestone P8.3)."""

import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit


# Known blocked local and metadata hostnames
BLOCKED_HOSTNAMES = frozenset(
    [
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
        "instance-data",
        "169.254.169.254",
    ]
)

# Allowed schemes for sandboxed browser navigation
ALLOWED_SCHEMES = frozenset(["http", "https"])


def normalize_origin(url_or_origin: str) -> str:
    """
    Extracts and normalizes the standard origin (scheme://host[:port]) from a URL or origin string.
    Rejects unsupported schemes, embedded credentials, and malformed inputs.
    """
    if not url_or_origin or not isinstance(url_or_origin, str):
        raise ValueError("URL or origin must be a non-empty string.")

    parsed = urlsplit(url_or_origin.strip())
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError(
            f"Unsupported scheme '{parsed.scheme}'. Only 'http' and 'https' are permitted."
        )

    if parsed.username or parsed.password:
        raise ValueError("Origins or URLs with embedded credentials are not permitted.")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL or origin must include a valid hostname.")

    hostname = hostname.lower()

    # Normalize default ports (omit 80 for http, 443 for https)
    port = parsed.port
    if port is not None:
        if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
            port = None

    port_str = f":{port}" if port is not None else ""
    return f"{scheme}://{hostname}{port_str}"


class OriginAllowlistManager:
    """
    Server-side allowlist manager for permitted web navigation origins.
    Enforces exact origin matching without wildcard expansion.
    """

    def __init__(self, raw_origins: list[str] | None = None):
        self._allowed_origins: set[str] = set()
        if raw_origins:
            for item in raw_origins:
                item_str = str(item).strip()
                if not item_str:
                    continue
                if "*" in item_str:
                    raise ValueError(
                        f"Wildcard origins are not permitted: '{item_str}'. Explicit origins required."
                    )
                norm = normalize_origin(item_str)
                self._allowed_origins.add(norm)

    def is_empty(self) -> bool:
        """Returns True if the allowlist has zero approved origins."""
        return len(self._allowed_origins) == 0

    def get_allowed_origins(self) -> list[str]:
        """Returns sorted list of approved origins."""
        return sorted(self._allowed_origins)

    def is_origin_allowed(self, url: str) -> tuple[bool, str | None]:
        """
        Validates whether the origin of the target URL is in the server allowlist.
        Returns (True, None) if permitted, or (False, reason) if rejected.
        """
        if self.is_empty():
            return False, "Browser origin allowlist is empty. No browser navigation is authorized."

        try:
            origin = normalize_origin(url)
        except ValueError as err:
            return False, f"URL validation failed: {str(err)}"

        if origin not in self._allowed_origins:
            return (
                False,
                f"Origin '{origin}' is not permitted by the server-side browser allowlist.",
            )

        return True, None


def validate_safe_destination(
    url: str,
    allow_private: bool = False,
    skip_dns: bool = False,
    mock_dns: dict[str, str | list[str]] | None = None,
) -> tuple[bool, str | None]:
    """
    Performs comprehensive SSRF checks on the target URL destination.
    Validates scheme, rejects embedded credentials, inspects IP literals,
    and resolves destination hostnames against loopback, private, link-local,
    multicast, and cloud metadata IP ranges.
    Returns (True, None) if safe, or (False, reason) if rejected.
    """
    try:
        parsed = urlsplit(url.strip())
    except Exception as exc:
        return False, f"Malformed URL: {str(exc)}"

    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        return (
            False,
            f"Unsupported scheme '{parsed.scheme}'. Only 'http' and 'https' are permitted.",
        )

    if parsed.username or parsed.password:
        return False, "URLs containing embedded credentials are not permitted."

    hostname = parsed.hostname
    if not hostname:
        return False, "URL hostname is missing or empty."

    hostname_clean = hostname.strip().lower()

    if hostname_clean in BLOCKED_HOSTNAMES and not allow_private:
        return (
            False,
            f"Destination hostname '{hostname_clean}' is a restricted local or cloud metadata name.",
        )

    # Check if host is an IP literal
    # Strip brackets if IPv6
    raw_ip_str = hostname_clean
    if raw_ip_str.startswith("[") and raw_ip_str.endswith("]"):
        raw_ip_str = raw_ip_str[1:-1]

    try:
        ip = ipaddress.ip_address(raw_ip_str)
        if not allow_private:
            if ip.is_loopback:
                return False, f"Destination IP '{ip}' is in the restricted loopback address range."
            if ip.is_private:
                return False, f"Destination IP '{ip}' is in a restricted private RFC1918/RFC4193 range."
            if ip.is_link_local:
                return False, f"Destination IP '{ip}' is in the restricted link-local address range."
            if ip.is_reserved:
                return False, f"Destination IP '{ip}' is in a reserved address range."
            if ip.is_multicast:
                return False, f"Destination IP '{ip}' is a multicast address."
            if ip.is_unspecified:
                return False, f"Destination IP '{ip}' is an unspecified address."
            if str(ip) == "169.254.169.254":
                return False, "Destination IP is the cloud metadata service."
        return True, None
    except ValueError:
        # Not an IP literal; proceed to DNS resolution check
        pass

    resolved_ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []

    if mock_dns and hostname_clean in mock_dns:
        raw_targets = mock_dns[hostname_clean]
        if isinstance(raw_targets, str):
            raw_targets = [raw_targets]
        for t in raw_targets:
            try:
                resolved_ips.append(ipaddress.ip_address(t))
            except ValueError:
                pass
    elif skip_dns:
        resolved_ips = []
    else:
        # Resolve hostname to check all resolved IP addresses against SSRF ranges
        port = parsed.port or (443 if scheme == "https" else 80)
        try:
            addr_info = socket.getaddrinfo(
                hostname_clean,
                port,
                proto=socket.IPPROTO_TCP,
            )
        except socket.gaierror as exc:
            return False, f"Destination hostname '{hostname_clean}' could not be resolved: {str(exc)}"
        except Exception as exc:
            return False, f"DNS resolution check failed for '{hostname_clean}': {str(exc)}"

        if not addr_info:
            return False, f"No IP addresses resolved for hostname '{hostname_clean}'."

        for entry in addr_info:
            sockaddr = entry[4]
            ip_str = sockaddr[0]
            try:
                resolved_ips.append(ipaddress.ip_address(ip_str))
            except ValueError:
                continue

    if not allow_private:
        for resolved_ip in resolved_ips:
            if resolved_ip.is_loopback:
                return (
                    False,
                    f"Destination hostname '{hostname_clean}' resolves to loopback IP '{resolved_ip}'.",
                )
            if resolved_ip.is_private:
                return (
                    False,
                    f"Destination hostname '{hostname_clean}' resolves to private IP '{resolved_ip}'.",
                )
            if resolved_ip.is_link_local:
                return (
                    False,
                    f"Destination hostname '{hostname_clean}' resolves to link-local IP '{resolved_ip}'.",
                )
            if resolved_ip.is_reserved:
                return (
                    False,
                    f"Destination hostname '{hostname_clean}' resolves to reserved IP '{resolved_ip}'.",
                )
            if resolved_ip.is_multicast:
                return (
                    False,
                    f"Destination hostname '{hostname_clean}' resolves to multicast IP '{resolved_ip}'.",
                )
            if resolved_ip.is_unspecified:
                return (
                    False,
                    f"Destination hostname '{hostname_clean}' resolves to unspecified IP '{resolved_ip}'.",
                )
            if str(resolved_ip) == "169.254.169.254":
                return (
                    False,
                    f"Destination hostname '{hostname_clean}' resolves to cloud metadata IP '{resolved_ip}'.",
                )

    return True, None


def sanitize_url(url: str, redact_query: bool = True) -> str:
    """
    Sanitizes a URL for diagnostic output and audit logging.
    Strips query string and fragment to prevent credential and token leaks.
    """
    try:
        parsed = urlsplit(url.strip())
        query = "" if redact_query else parsed.query
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))
    except Exception:
        return url
