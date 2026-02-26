from __future__ import annotations

import os

from lib.runtime_common import (
    env_str,
    load_plaintext_secret_file,
    load_powershell_dpapi_securestring_file,
)


def build_clob_client_from_env(
    clob_host: str,
    chain_id: int,
    missing_env_message: str,
    invalid_key_message: str,
):
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
    except ImportError as e:
        raise RuntimeError("py-clob-client is not installed. Run: python -m pip install py-clob-client") from e

    private_key = (env_str("PM_PRIVATE_KEY") or "").strip()
    if not private_key:
        private_key = load_plaintext_secret_file(env_str("PM_PRIVATE_KEY_FILE"), mode="hex_private_key")
    if not private_key:
        private_key = load_powershell_dpapi_securestring_file(env_str("PM_PRIVATE_KEY_DPAPI_FILE"))
    funder = env_str("PM_FUNDER") or env_str("PM_PROXY_ADDRESS")
    signature_type = int(os.environ.get("PM_SIGNATURE_TYPE", "0"))

    if private_key and not private_key.startswith("0x") and len(private_key) == 64:
        private_key = "0x" + private_key

    if not private_key or not funder:
        raise RuntimeError(missing_env_message)

    if not ((private_key.startswith("0x") and len(private_key) == 66) or (len(private_key) == 64)):
        raise RuntimeError(invalid_key_message)

    client = ClobClient(
        host=clob_host,
        chain_id=int(chain_id),
        key=private_key,
        signature_type=signature_type,
        funder=funder,
    )

    k = env_str("PM_API_KEY")
    s = env_str("PM_API_SECRET")
    p = env_str("PM_API_PASSPHRASE")
    if not s:
        s = load_powershell_dpapi_securestring_file(env_str("PM_API_SECRET_DPAPI_FILE"))
    if not p:
        p = load_powershell_dpapi_securestring_file(env_str("PM_API_PASSPHRASE_DPAPI_FILE"))
    if k and s and p:
        client.set_api_creds(ApiCreds(api_key=k, api_secret=s, api_passphrase=p))
    else:
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
    return client

