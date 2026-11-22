from dataclasses import dataclass

import jwt
from jwt import PyJWKClient
from jwt.exceptions import InvalidTokenError, PyJWKClientConnectionError, PyJWKClientError

from ecommerce_store_agent_orchestrator.domain.runs import RunOwner


class InvalidAccessTokenError(Exception):
    pass


class IdentityProviderUnavailableError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Principal:
    owner: RunOwner
    scopes: frozenset[str]


class AccessTokenVerifier:
    def __init__(self, *, issuer: str, audience: str, jwks_url: str) -> None:
        self._issuer = issuer
        self._audience = audience
        self._jwks = PyJWKClient(jwks_url, timeout=5)

    def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
                raise InvalidAccessTokenError
            key = self._jwks.get_signing_key(kid)
        except (PyJWKClientConnectionError, ValueError) as exc:
            raise IdentityProviderUnavailableError from exc
        except (InvalidTokenError, PyJWKClientError) as exc:
            raise InvalidAccessTokenError from exc

        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                issuer=self._issuer,
                audience=self._audience,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except (InvalidTokenError, TypeError, ValueError) as exc:
            raise InvalidAccessTokenError from exc

        subject = claims["sub"]
        if not isinstance(subject, str) or not subject.strip():
            raise InvalidAccessTokenError
        scope = claims.get("scp")
        scopes = frozenset(scope.split()) if isinstance(scope, str) else frozenset()
        return Principal(RunOwner(self._issuer, subject), scopes)
