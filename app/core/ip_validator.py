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
        True if IP is blocked, False if safe
    """
    try:
        addrinfo = socket.getaddrinfo(
            hostname, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
        )
    except socket.gaierror:
        return True

    if not addrinfo:
        return True

    for _family, _type, _proto, _canonname, sockaddr in addrinfo:
        try:
            ip = ip_address(sockaddr[0])
        except ValueError:
            return True
        if _is_unsafe_ip(ip):
            return True

    return False

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
        ValueError: If URL is unsafe
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname

        if not hostname:
            raise ValueError("Invalid URL: no hostname")

        if is_ip_blocked(hostname):
            raise ValueError(
                f"URL resolves to blocked/private IP. "
                f"Cannot scrape internal services: {hostname}"
            )

    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"URL validation failed: {str(e)}")
