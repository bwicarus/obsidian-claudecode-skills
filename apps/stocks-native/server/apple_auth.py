"""Minimal Sign in with Apple identity-token verification for the native app."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any

import aiohttp
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from auth import AuthError


def _b64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class AppleIdentityVerifier:
    def __init__(self, audience: str, *, clock=time.time):
        self.audience = audience
        self.clock = clock
        self._keys: dict[str, dict[str, Any]] = {}
        self._keys_at = 0.0

    async def _key(self, kid: str) -> dict[str, Any]:
        now = self.clock()
        if kid not in self._keys or now - self._keys_at > 6 * 3600:
            timeout = aiohttp.ClientTimeout(total=8, connect=3)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get("https://appleid.apple.com/auth/keys") as response:
                    response.raise_for_status()
                    payload = await response.json(content_type=None)
            self._keys = {str(key.get("kid")): key for key in payload.get("keys", []) if key.get("kid")}
            self._keys_at = now
        key = self._keys.get(kid)
        if not key:
            raise AuthError("Unknown Apple signing key")
        return key

    async def verify(self, identity_token: str, raw_nonce: str) -> dict[str, Any]:
        if not isinstance(identity_token, str) or len(identity_token) > 16_384:
            raise AuthError("Invalid Apple identity token")
        if not isinstance(raw_nonce, str) or not 32 <= len(raw_nonce) <= 128:
            raise AuthError("Invalid Apple nonce")
        try:
            encoded_header, encoded_claims, encoded_signature = identity_token.split(".")
            header = json.loads(_b64(encoded_header))
            claims = json.loads(_b64(encoded_claims))
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise AuthError("Invalid Apple token algorithm")
            key = await self._key(header["kid"])
            public_key = rsa.RSAPublicNumbers(
                int.from_bytes(_b64(key["e"]), "big"), int.from_bytes(_b64(key["n"]), "big")
            ).public_key()
            public_key.verify(_b64(encoded_signature),
                              f"{encoded_header}.{encoded_claims}".encode(),
                              padding.PKCS1v15(), hashes.SHA256())
        except AuthError:
            raise
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise AuthError("Invalid Apple identity token") from exc
        now = self.clock()
        audience = claims.get("aud")
        valid_audience = self.audience in audience if isinstance(audience, list) else audience == self.audience
        expected_nonce = hashlib.sha256(raw_nonce.encode()).hexdigest()
        if claims.get("iss") != "https://appleid.apple.com" or not valid_audience:
            raise AuthError("Invalid Apple token issuer or audience")
        if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] <= now:
            raise AuthError("Expired Apple identity token")
        if not isinstance(claims.get("iat"), (int, float)) or claims["iat"] > now + 120:
            raise AuthError("Invalid Apple token time")
        if claims.get("nonce") != expected_nonce:
            raise AuthError("Invalid Apple nonce")
        if not isinstance(claims.get("sub"), str):
            raise AuthError("Invalid Apple subject")
        return claims
