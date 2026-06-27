"""IP validation to prevent SSRF (Server-Side Request Forgery) attacks."""

import socket
from ipaddress import ip_address, ip_network
from urllib.parse import urlparse

BLOCKED_IP_RANGES = [
    ip_network("127.0.0.0/8"),
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("169.254.0.0/16"),
    ip_network("0.0.0.0/8"),
    ip_network("224.0.0.0/4"),
    ip_network("240.0.0.0/4"),
    ip_network("255.255.255.255/32"),
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
        ip_str = socket.gethostbyname(hostname)
        ip = ip_address(ip_str)

        for blocked_range in BLOCKED_IP_RANGES:
            if ip in blocked_range:
                return True

        return False

    except socket.gaierror:
        return True
    except ValueError:
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
