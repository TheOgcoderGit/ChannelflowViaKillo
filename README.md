# ChannelFlow AI

ChannelFlow AI is a Telegram-controlled forwarding service baseline. This
repository now contains the imported legacy project, its master requirements,
and an implementation tracker for a phased rebuild.

## Project documents

- [Product requirements (`PRD.md`)](PRD.md) — the single source of truth for
  the required product behavior and implementation order.
- [Implementation tracker](PROJECT_TRACKER.md) — the current rebuilding plan.
- [Feature Reality Matrix](docs/FEATURE_REALITY_MATRIX.md) — what is present in
  the imported baseline versus what still needs verification or implementation.

## Current status

The legacy source has been imported, but it is **not** certified as a complete
or production-ready implementation of the PRD. The first implementation work
addresses the P0 Telegram connection flow: `FLOW <OTP>` input, expiry handling,
encrypted session storage, and database-enforced uniqueness for an external
Telegram account's active owner.

Do not enter Telegram credentials until the deployment has been configured
with its own `.env` file and a valid `SESSION_ENCRYPTION_KEY`.

## Local setup

1. Create a virtual environment and install dependencies:

   ```bash
   python -m pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and configure only your own credentials.
3. Initialize the database:

   ```bash
   python -m database.schema
   ```

4. Run the bot:

   ```bash
   python main.py
   ```

## Checks

Run the dependency-free database ownership regression test with:

```bash
python -m unittest tests/test_database_integrity.py
```

Then run the full verification sequence defined in the PRD once dependencies
and external test accounts are available. Never commit `.env`, Telethon
sessions, or `channelflow.db`.
