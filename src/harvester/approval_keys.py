"""Verify RS256 approval records signed by the web app's pinned Key Vault key.

The web app signs SHA-256 of the canonical record (without its signature) with RS256 through Key Vault.
The harvester only verifies: it reads the public half of the one pinned key version and never signs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .core import HarvestError, canonical_json


@dataclass(frozen=True)
class RS256ApprovalVerifier:
    key_id: str
    public_key: rsa.RSAPublicKey

    @classmethod
    def from_numbers(cls, key_id: str, *, n: bytes, e: bytes) -> RS256ApprovalVerifier:
        numbers = rsa.RSAPublicNumbers(
            int.from_bytes(e, "big"), int.from_bytes(n, "big")
        )
        return cls(key_id=key_id, public_key=numbers.public_key())

    def __call__(self, record: dict[str, Any]) -> bool:
        if record.get("algorithm") != "RS256" or record.get("keyId") != self.key_id:
            return False
        signature = record.get("signature")
        if not isinstance(signature, str):
            return False
        try:
            raw = bytes.fromhex(signature)
        except ValueError:
            return False
        payload = {key: value for key, value in record.items() if key != "signature"}
        try:
            self.public_key.verify(
                raw,
                canonical_json(payload).encode("utf-8"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except InvalidSignature:
            return False
        return True


def parse_key_id(key_id: str) -> tuple[str, str, str]:
    """Split a versioned Key Vault key ID into (vault URL, key name, version)."""
    parsed = urlparse(key_id)
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or len(parts) != 3
        or parts[0] != "keys"
        or not all(parts[1:])
    ):
        raise HarvestError("The approval key ID must be a versioned Key Vault key URL.")
    return f"https://{parsed.hostname}", parts[1], parts[2]


def load_key_vault_verifier(
    key_id: str, *, credential: Any | None = None, key_client: Any | None = None
) -> RS256ApprovalVerifier:
    """Read the public half of the pinned approval key version from Key Vault."""
    vault_url, name, version = parse_key_id(key_id)
    if key_client is None:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.keys import KeyClient

        key_client = KeyClient(vault_url, credential or DefaultAzureCredential())
    try:
        key = key_client.get_key(name, version)
    except Exception as exc:  # noqa: BLE001 - any Key Vault failure fails closed
        raise HarvestError("The approval verification key is unavailable.") from exc
    if key.id != key_id:
        raise HarvestError("Key Vault returned a different approval key version.")
    jwk = key.key
    if getattr(jwk, "kty", None) not in ("RSA", "RSA-HSM"):
        raise HarvestError("The approval verification key is not an RSA key.")
    if not jwk.n or not jwk.e:
        raise HarvestError("The approval verification key has no public numbers.")
    return RS256ApprovalVerifier.from_numbers(key_id, n=bytes(jwk.n), e=bytes(jwk.e))
