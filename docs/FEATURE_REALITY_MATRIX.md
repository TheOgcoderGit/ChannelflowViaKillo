# Feature Reality Matrix

This is an audit record for the imported legacy baseline, not a claim that all
features are complete. `PRD.md` remains the source of truth.

| PRD area | Baseline evidence | Status | Required follow-up |
| --- | --- | --- | --- |
| P0 Telegram login | `core/user_sessions.py`, `bot/handlers.py` | In progress | FLOW OTP input, expiry, encrypted sessions, cancellation cleanup, and external-account ownership are implemented; test with real Telegram credentials. |
| P0 account ownership | `database/db.py`, `core/user_sessions.py`, `core/client_pool.py` | In progress | SQLite preserves one owner per external Telegram ID; invalid/decryption-failed sessions are marked `reconnect_required` during startup recovery without releasing their owner claim. Add integration/race tests around real login and reconnect. |
| P0 project engine | `services/project_service.py`, `core/forwarder.py`, `services/plan_service.py` | In progress | Removed duplicate/unreachable quota reservation from dispatch; SQLite quota reservation is concurrency-tested. Audit CRUD, permissions, media, deduplication, retry, and quota end-to-end. |
| P0 WhatsApp | `services/whatsapp_pairing.py`, `destinations/whatsapp_destination.py` | Needs verification | Validate pairing, reconnect, ownership, and live delivery. |
| P1 transformations | `services/content_rules_service.py`, `services/formatting_service.py`, `services/ai_service.py`, `services/watermark_service.py` | Needs verification | Compare ordering and failure policy to PRD. |
| P1 monetization | `services/plan_service.py`, `services/payment_service.py`, `services/wallet_service.py` | Needs redesign | The imported tests describe per-forward charging, which conflicts with the PRD's explicit no-per-forward-billing rule. |
| P1 support/admin | `services/support_service.py`, `bot/admin_panel.py` | Needs verification | Verify RBAC, idempotency, audit records, and live operations. |
| P2 operations | `services/log_service.py`, `core/listener.py` | Needs verification | Audit localization, shutdown, rate limits, health checks, and Pydroid smoke test. |

## Audit constraints

- Never treat a module's presence as evidence of working behavior.
- Never commit credentials, sessions, or a runtime database.
- Implement and verify the phases in the order in `PRD.md`.
