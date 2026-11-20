"""IP validation to prevent SSRF (Server-Side Request Forgery) attacks."""

import socket
from ipaddress import ip_address
from urllib.parse import urlparse

def _is_unsafe_ip(ip) -> bool:
    """
    True if this address (IPv4 or IPv6) points somewhere internal/private/
    reserved that shouldn't be reachable from a server-side fetch. Using the
    stdlib ipaddress properties instead of a hand-rolled CIDR list means
    IPv6 gets the same coverage as IPv4 for free — cloud metadata endpoints
    (169.254.169.254, fd00:ec2::254) are already caught by is_link_local /
    is_private respectively, verified empirically rather than assumed.
    """
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )

class DnsResolutionError(ValueError):
    """The hostname itself could not be resolved — a dead/typo'd domain,
    not a security rejection. Kept as a ValueError subclass so existing
    `except ValueError` call sites still catch it, while callers that want
    to show a more accurate message than "blocked" can catch this specifically."""


def _resolve(hostname: str) -> list[tuple[int, str]]:
    """(family, ip-string) for every address `hostname` resolves to. Raises
    DnsResolutionError — not the generic "blocked" ValueError — when the name
    simply doesn't resolve (NXDOMAIN, typo, dead domain), so that failure
    isn't misreported as a security rejection."""
    try:
        addrinfo = socket.getaddrinfo(
            hostname, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
        )
    except socket.gaierror as e:
        raise DnsResolutionError(f"Could not resolve hostname: {hostname}") from e
    if not addrinfo:
        raise DnsResolutionError(f"Could not resolve hostname: {hostname}")

    resolved: list[tuple[int, str]] = []
    for family, _type, _proto, _canonname, sockaddr in addrinfo:
        try:
            resolved.append((family, str(ip_address(sockaddr[0]))))
        except ValueError:
            raise ValueError(f"URL resolves to an unparseable address: {hostname}")
    return resolved

def is_ip_blocked(hostname: str) -> bool:
    """
    Check if hostname resolves to a blocked IP address — IPv4 or IPv6, and
    every address it resolves to, not just the first one returned. A
    hostname can have multiple A/AAAA records; an attacker only needs one
    of them to point somewhere unsafe (the previous IPv4-only,
    single-address check via socket.gethostbyname missed both cases).

    Args:
        hostname: Domain name or IP address

    Returns:
        True if IP is blocked (including an unresolvable hostname — callers
        that need to tell "blocked" apart from "doesn't exist" should use
        resolve_safe_ip instead, which raises DnsResolutionError for the
        latter), False if safe.
    """
    try:
        resolved = _resolve(hostname)
    except DnsResolutionError:
        return True

    return any(_is_unsafe_ip(ip_address(ip)) for _family, ip in resolved)

def resolve_safe_ip(hostname: str) -> str:
    """
    Resolve `hostname` and return ONE validated-safe literal IP address
    (IPv4 preferred for broadest compatibility), or raise.

    Same "block if ANY resolved address is unsafe" policy as is_ip_blocked
    (a hostname that resolves to a mix of public and private addresses is
    itself suspicious — don't let a later, different lookup pick the bad
    one). The difference is this hands back the address it validated, so
    the caller can connect to that exact IP instead of trusting the HTTP
    client to re-resolve the hostname a moment later — closing the DNS-
    rebinding gap where a second lookup could legitimately return a
    different, unsafe address than the first one just checked.

    Raises:
        DnsResolutionError: the hostname doesn't resolve at all.
        ValueError: it resolves, but to a blocked/private/unparseable address.
    """
    resolved = _resolve(hostname)  # raises DnsResolutionError if unresolvable
    for _family, ip in resolved:
        if _is_unsafe_ip(ip_address(ip)):
            raise ValueError(
                f"URL resolves to blocked/private IP. Cannot scrape internal services: {hostname}"
            )
    resolved.sort(key=lambda t: 0 if t[0] == socket.AF_INET else 1)
    return resolved[0][1]

def validate_url_safe(url: str) -> None:
    """
    Validate that URL is safe to scrape.

    Prevents:
    - Scraping internal/private IPs
    - Accessing localhost services
    - SSRF attacks

    Args:
        url: URL to validate

    Raises:
        ValueError: If the URL is unsafe, or its hostname doesn't resolve
            (DnsResolutionError, a ValueError subclass, with an accurate
            "could not resolve" message rather than a misleading "blocked"
            one — see _resolve).
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname

        if not hostname:
            raise ValueError("Invalid URL: no hostname")

        resolve_safe_ip(hostname)  # raises with the accurate DNS-vs-blocked message

    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"URL validation failed: {str(e)}")
