"""
Cliente HTTP crudo hacia Microsoft Entra ID (OAuth2 / OpenID Connect).

TODO: implementar el intercambio `authorization_code` -> tokens contra el
endpoint /oauth2/v2.0/token del tenant de la UVG.
"""


async def exchange_code_for_tokens(code: str, redirect_uri: str) -> dict:
    raise NotImplementedError("Pendiente: integración con Microsoft Entra ID.")
