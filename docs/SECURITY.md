# Security Notes (Do Not Skip)

This repo contains automation that can place trades. Treat it like production ops code.

## Never Commit Secrets

- Do not commit:
  - private keys / seed phrases
  - API keys (Simmer / Polymarket)
  - Discord webhook URLs

Recommended:
- Keep secrets in **User environment variables** or OS-protected secret stores.

## Polymarket CLOB Key Handling (Windows)

Recommended pattern:
- Store the private key as a DPAPI-encrypted SecureString file (PowerShell `ConvertFrom-SecureString`)
- Point the bot at it via `PM_PRIVATE_KEY_DPAPI_FILE`

Do not print these values in chat or logs.

## Use a Dedicated Hot Wallet

- Use a separate wallet for bots.
- Fund with the minimum necessary.
- Periodically sweep profits to cold storage.

## Discord Webhooks

- Webhook URLs allow anyone who obtains the URL to post messages into your Discord channel.
- Treat webhook URLs as secrets.

## Principle of Least Privilege

- Keep `--execute` off until observation confirms stable behavior.
- Enforce explicit confirmation flags (`--confirm-live YES`) for live execution modes.

