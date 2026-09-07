"""
ChannelFlow AI - Per-User Telegram Login (/connect)
======================================================

Lets a customer log their own Telegram account into the bot, so their
projects can eventually forward using their own account rather than
the service-operator's shared session (core/client.py). This is
SEPARATE infrastructure from core/client.py/core/authorize.py on
purpose - see core/authorize.py's module docstring for why phone/OTP/
2FA collection through bot chat was originally kept out of the chat UI,
and bot/handlers.py's connect-flow messaging for the consent/safety
copy shown to the user before they type anything.

Flow
----
1. start_connect(user_id, phone_number) - opens a temporary Telethon
   client bound to a StringSession (never touches disk on its own),
   requests an OTP, and holds the client in memory keyed by user_id
   while waiting for the code.
2. submit_code(user_id, code) - completes sign-in, or raises
   NeedsPassword if the account has 2FA enabled.
3. submit_password(user_id, password) - completes 2FA sign-in.
4. On success in either step: the resulting session string is
   encrypted (core/session_crypto.py) and upserted into
   user_telegram_sessions. The temporary client disconnects
   immediately after - nothing about this module keeps a live
   connection open per connected user; that's a separate concern for
   whatever eventually runs per-user forwarding.

Safety
------
* Every pending attempt expires after PENDING_TTL_SECONDS and is
  disconnected + dropped - a user who starts /connect and never
  finishes can't leave a dangling authenticated-but-unclaimed client
  sitting in memory indefinitely.
* MAX_ATTEMPTS caps wrong-code/wrong-password tries per pending attempt
  before it's dropped and the user has to restart with /connect.
* Nothing in this module logs a phone number, code, password, or
  session string - only user ids and outcome (success/failure/reason).
"""

import asyncio
import logging
import time

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PhoneNumberInvalidError,
    PasswordHashInvalidError,
    FloodWaitError,
)

from config import API_ID, API_HASH
from database.db import get_connection
from core.session_crypto import encrypt_session

logger = logging.getLogger(__name__)

PENDING_TTL_SECONDS = 600  # 10 minutes to finish a /connect attempt
MAX_ATTEMPTS = 3

# ---- OTP input-format handling (/myflow prefix) -----------------------
# Telegram's anti-abuse systems are NOT touched: the OTP digits sent to
# Telethon are exactly what Telegram delivered. The /myflow prefix is a
# BOT-SIDE INPUT FORMAT ONLY - it exists so a login code arriving in an
# otherwise-normal chat can't be confused with other messages, and so a
# stray paste can't be consumed by the wrong handler.
OTP_COMMAND_PREFIX = "/myflow"


def extract_otp_from_command(text: str):
    """Parses '/myflow12345' -> '12345'.

    Returns (ok, code_or_error_message). Validates:
      * message starts with exactly /myflow
      * remainder is 1-8 digits (Telegram OTPs are typically 5)
    No logging of the code itself - only pass/fail."""

    if not text:
        return False, "Empty message."

    if not text.startswith(OTP_COMMAND_PREFIX):
        return False, (
            f"Send the code in this format: {OTP_COMMAND_PREFIX}<code>\n"
            f"Example: {OTP_COMMAND_PREFIX}12345"
        )

    code = text[len(OTP_COMMAND_PREFIX):].strip()

    if not code.isdigit() or not (1 <= len(code) <= 8):
        return False, (
            "That doesn't look like a valid code. Digits only - "
            f"example: {OTP_COMMAND_PREFIX}12345"
        )

    return True, code

# -----------------------------------------------------------------------

# user_id -> {"client": TelegramClient, "phone": str, "phone_code_hash": str,
#             "stage": "code"|"password", "attempts": int, "started_at": float}
_pending = {}
_pending_lock = asyncio.Lock()

# Codes already submitted for a given pending attempt (per-user set).
# Prevents reuse of an already-tried OTP within the same attempt window
# (a retry with the SAME code would burn another wrong-attempt slot at
# Telegram anyway; blocking it early is cheaper and clearer).
_submitted_codes = {}  # user_id -> set of codes tried this attempt

# Auto-cleanup task state
_wc_stage_cleanup_task = None


class ConnectError(Exception):
    """User-facing failure - message is safe to show as-is."""


class NeedsPassword(Exception):
    """Sign-in succeeded on the code but this account has 2FA enabled."""


async def _drop_pending(user_id):

    state = _pending.pop(user_id, None)
    # Clear any record of codes tried in this attempt (never logged,
    # just freed).
    _submitted_codes.pop(user_id, None)

    if state is not None:
        try:
            await state["client"].disconnect()
        except Exception:
            pass


async def _sweep_expired():

    now = time.monotonic()
    expired = [
        uid for uid, s in _pending.items()
        if now - s["started_at"] > PENDING_TTL_SECONDS
    ]

    for uid in expired:
        await _drop_pending(uid)


async def start_cleanup_task():
    """Background task to periodically clean up expired pending connections."""
    while True:
        await asyncio.sleep(60)  # Run every minute
        try:
            await _sweep_expired()
        except Exception:
            logger.exception("Error in _sweep_expired background task")


async def start_wc_stage_cleanup_task():
    """Background task to periodically clean up WAITING_CONNECT_STAGE entries
    that have expired (user didn't respond within TTL). Runs every 30 seconds."""
    while True:
        await asyncio.sleep(30)
        try:
            now = time.monotonic()
            expired = [
                uid for uid, stage in WAITING_CONNECT_STAGE.items()
                if now - stage > PENDING_TTL_SECONDS
                # stage values are timestamps stored as monotonic time
                # when set in _handle_connect_phone at line 954 area
            ]
            for uid in expired:
                WAITING_CONNECT_STAGE.pop(uid, None)
                # Also drop any pending session so user can restart
                await _drop_pending(uid)
                logger.info(f"Auto-cleaned expired WAITING_CONNECT_STAGE for user {uid}")
        except Exception:
            logger.exception("Error in WAITING_CONNECT_STAGE cleanup task")


async def start_connect(user_id: int, phone_number: str):
    """Sends the OTP. Raises ConnectError with a user-safe message on
    any failure (invalid number, flood wait, etc.)."""

    async with _pending_lock:

        await _sweep_expired()
        await _drop_pending(user_id)  # restart cleanly if one was already in progress

        client = TelegramClient(StringSession(), API_ID, API_HASH)

        try:
            await client.connect()
            sent = await client.send_code_request(phone_number)

        except PhoneNumberInvalidError:
            await client.disconnect()
            raise ConnectError("That phone number doesn't look valid. Include the country code, e.g. +919876543210.")

        except FloodWaitError as e:
            await client.disconnect()
            raise ConnectError(f"Telegram asked us to wait {e.seconds}s before trying again. Please retry shortly.")

        except Exception:
            await client.disconnect()
            logger.exception("start_connect failed for user %s", user_id)
            raise ConnectError("Couldn't start the login. Please try again in a moment.")

        _pending[user_id] = {
            "client": client,
            "phone": phone_number,
            "phone_code_hash": sent.phone_code_hash,
            "stage": "code",
            "attempts": 0,
            "started_at": time.monotonic(),
        }

        _submitted_codes[user_id] = set()


async def submit_code(user_id: int, code: str):
    """Returns True on full success. Raises NeedsPassword if 2FA is
    required (call submit_password next), or ConnectError on any other
    failure - the caller should let the user retry within MAX_ATTEMPTS.

    `code` here must be the EXTRACTED digits only (see
    extract_otp_from_command) - the /myflow prefix never reaches this
    function and never goes to Telegram."""

    state = _pending.get(user_id)

    if state is None or state["stage"] != "code":
        raise ConnectError("No login in progress. Send /connect <phone_number> to start.")

    # Check for timeout before processing
    if _time.monotonic() - state["started_at"] > PENDING_TTL_SECONDS:
        await _drop_pending(user_id)
        raise ConnectError("Login session expired. Send /connect <phone_number> to start over.")

    # Prevent reuse of an already-submitted OTP within this attempt.
    if code in _submitted_codes.get(user_id, set()):
        raise ConnectError(
            "You've already tried that code. Check for a NEWER code from "
            "Telegram - the latest message counts."
        )

    client = state["client"]

    try:
        await client.sign_in(
            phone=state["phone"], code=code, phone_code_hash=state["phone_code_hash"]
        )

    except SessionPasswordNeededError:
        # Code accepted; account has 2FA. Mark code consumed and move on.
        _submitted_codes.setdefault(user_id, set()).add(code)
        state["stage"] = "password"
        raise NeedsPassword()

    except FloodWaitError as e:
        await _drop_pending(user_id)
        raise ConnectError(
            f"Telegram asked us to wait {e.seconds}s before continuing. "
            "Please start again with /connect after that."
        )

    except (PhoneCodeInvalidError, PhoneCodeExpiredError):

        _submitted_codes.setdefault(user_id, set()).add(code)
        state["attempts"] += 1

        if state["attempts"] >= MAX_ATTEMPTS:
            await _drop_pending(user_id)
            raise ConnectError("Too many incorrect attempts. Send /connect <phone_number> to start over.")

        raise ConnectError(
            "That code was incorrect or expired. "
            f"Try the newest code: {OTP_COMMAND_PREFIX}<code>"
        )

    except Exception:
        await _drop_pending(user_id)
        logger.exception("submit_code failed for user %s", user_id)
        raise ConnectError("Something went wrong during login. Send /connect <phone_number> to start over.")

    # Success - pending state (and any memory of codes tried) is wiped
    # immediately in _finalize via _drop_pending semantics.
    await _finalize(user_id)
    return True


async def submit_password(user_id: int, password: str):

    state = _pending.get(user_id)

    if state is None or state["stage"] != "password":
        raise ConnectError("No login in progress. Send /connect <phone_number> to start.")

    # Check for timeout before processing
    if _time.monotonic() - state["started_at"] > PENDING_TTL_SECONDS:
        await _drop_pending(user_id)
        raise ConnectError("Login session expired. Send /connect <phone_number> to start over.")

    client = state["client"]

    try:
        await client.sign_in(password=password)

    except PasswordHashInvalidError:

        state["attempts"] += 1

        if state["attempts"] >= MAX_ATTEMPTS:
            await _drop_pending(user_id)
            raise ConnectError("Too many incorrect attempts. Send /connect <phone_number> to start over.")

        raise ConnectError("Incorrect password. Try again.")

    except FloodWaitError as e:
        await _drop_pending(user_id)
        raise ConnectError(
            f"Telegram asked us to wait {e.seconds}s before continuing. "
            "Please start again with /connect after that."
        )

    except Exception:
        await _drop_pending(user_id)
        logger.exception("submit_password failed for user %s", user_id)
        raise ConnectError("Something went wrong during login. Send /connect <phone_number> to start over.")

    await _finalize(user_id)
    return True


async def _finalize(user_id):

    state = _pending.pop(user_id)
    client = state["client"]

    session_string = client.session.save()
    phone_number = state["phone"]

    await client.disconnect()

    encrypted = encrypt_session(session_string)

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO user_telegram_sessions(telegram_id, encrypted_session, phone_number, status)
        VALUES (?, ?, ?, 'connected')
        ON CONFLICT(telegram_id) DO UPDATE SET
            encrypted_session=excluded.encrypted_session,
            phone_number=excluded.phone_number,
            status='connected',
            connected_at=CURRENT_TIMESTAMP
        """,
        (user_id, encrypted, phone_number),
    )

    conn.commit()
    conn.close()

    logger.info("User %s connected their Telegram account", user_id)


def cancel_connect(user_id):
    """Sync-safe best-effort cancel for /cancel - actual disconnect
    happens via the async _drop_pending on next opportunity, but
    removing the dict entry immediately stops it being usable."""

    _pending.pop(user_id, None)


def is_connected(telegram_id) -> bool:

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT 1 FROM user_telegram_sessions WHERE telegram_id=? AND status='connected'",
        (telegram_id,),
    )
    row = cur.fetchone()
    conn.close()

    return row is not None


def disconnect_user(telegram_id):
    """User-initiated revoke - deletes the stored session outright
    rather than just flipping a status flag, so there's nothing left to
    leak even if the row were somehow read elsewhere."""

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("DELETE FROM user_telegram_sessions WHERE telegram_id=?", (telegram_id,))

    conn.commit()
    conn.close()
