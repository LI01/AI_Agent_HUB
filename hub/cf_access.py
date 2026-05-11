"""Cloudflare Access JWT verification — alternate auth path for read endpoints.

When `AGENT_HUB_CF_ACCESS_TEAM_DOMAIN` and `AGENT_HUB_CF_ACCESS_AUD` are set,
the hub accepts a verified `Cf-Access-Jwt-Assertion` header in place of a
bearer token, for view endpoints only (see `require_view_*` in main.py).
Bearer remains the only auth path for mutating endpoints.

JWKS is fetched on first use and cached for 1 hour. Verification checks
signature (RS256), issuer, audience, and expiry. Email-domain filtering is
defense-in-depth — Cloudflare Access already enforces the email policy at
the edge, but a misconfigured policy shouldn't grant hub access.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional
from urllib.request import urlopen

try:
    import jwt as _jwt
    from jwt.algorithms import RSAAlgorithm
except ImportError:  # pragma: no cover — pyjwt is in requirements.txt
    _jwt = None
    RSAAlgorithm = None


class CfAccessVerifier:
    def __init__(
        self,
        team_domain: str,
        audience: str,
        allowed_email_domains: Optional[list[str]] = None,
        jwks_ttl: int = 3600,
    ):
        self.team_domain = team_domain.rstrip("/")
        self.audience = audience
        self.allowed_email_domains = [
            d.lower().lstrip("@") for d in (allowed_email_domains or []) if d.strip()
        ]
        self._jwks_ttl = jwks_ttl
        self._jwks: Optional[dict] = None
        self._jwks_fetched_at: float = 0.0

    def _fetch_jwks(self) -> dict:
        url = f"https://{self.team_domain}/cdn-cgi/access/certs"
        with urlopen(url, timeout=5) as resp:
            return json.loads(resp.read())

    def _get_jwks(self) -> dict:
        if (
            self._jwks is None
            or (time.time() - self._jwks_fetched_at) > self._jwks_ttl
        ):
            self._jwks = self._fetch_jwks()
            self._jwks_fetched_at = time.time()
        return self._jwks

    def verify(self, token: str) -> Optional[dict]:
        """Return decoded claims if `token` is a valid CF Access JWT, else None.

        Returns None on any verification failure (bad signature, expired,
        wrong audience, missing email, disallowed domain).
        """
        if _jwt is None or RSAAlgorithm is None:
            return None
        try:
            header = _jwt.get_unverified_header(token)
            kid = header.get("kid")
            key = None
            for k in self._get_jwks().get("keys", []):
                if k.get("kid") == kid:
                    key = RSAAlgorithm.from_jwk(json.dumps(k))
                    break
            if key is None:
                return None
            claims = _jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=f"https://{self.team_domain}",
            )
        except Exception:
            return None

        email = (claims.get("email") or "").lower().strip()
        if not email:
            return None
        if self.allowed_email_domains:
            domain = email.rsplit("@", 1)[-1] if "@" in email else ""
            if domain not in self.allowed_email_domains:
                return None
        return claims


def from_env() -> Optional[CfAccessVerifier]:
    """Construct a verifier from env vars, or None if not configured."""
    team = os.getenv("AGENT_HUB_CF_ACCESS_TEAM_DOMAIN", "").strip()
    aud = os.getenv("AGENT_HUB_CF_ACCESS_AUD", "").strip()
    if not team or not aud:
        return None
    allowed = [
        d.strip()
        for d in os.getenv("AGENT_HUB_CF_ACCESS_ALLOWED_DOMAINS", "").split(",")
        if d.strip()
    ]
    return CfAccessVerifier(team, aud, allowed)
