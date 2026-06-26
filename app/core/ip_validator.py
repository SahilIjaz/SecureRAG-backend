"""IP validation to prevent SSRF (Server-Side Request Forgery) attacks."""

import socket
from ipaddress import ip_address, ip_network
from urllib.parse import urlparse


BLOCKED_IP_RANGES = [
    ip_network("127.0.0.0/8"),       # Localhost
    ip_network("10.0.0.0/8"),        # Private network
    ip_network("172.16.0.0/12"),     # Private network
    ip_network("192.168.0.0/16"),    # Private network
    ip_network("169.254.0.0/16"),    # Link-local
    ip_network("0.0.0.0/8"),         # This network
    ip_network("224.0.0.0/4"),       # Multicast
    ip_network("240.0.0.0/4"),       # Reserved
    ip_network("255.255.255.255/32"), # Broadcast
]


def is_ip_blocked(hostname: str) -> bool:
    """
    Check if hostname resolves to a blocked IP address.

    Args:
        hostname: Domain name or IP address

    Returns:
        True if IP is blocked, False if safe
    """
    try:
        # Resolve hostname to IP
        ip_str = socket.gethostbyname(hostname)
        ip = ip_address(ip_str)

        # Check against blocked ranges
        for blocked_range in BLOCKED_IP_RANGES:
            if ip in blocked_range:
                return True

        return False

    except socket.gaierror:
        # DNS lookup failed - block it for safety
        return True
    except ValueError:
        # Invalid IP format - block it for safety
        return True


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
