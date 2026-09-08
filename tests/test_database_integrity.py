"""Regression checks for database-enforced account ownership."""

import os
import tempfile
import unittest


class TelegramOwnershipSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test.db")
        self.old_db_name = os.environ.get("DB_NAME")
        os.environ["DB_NAME"] = self.db_path
        import database.db as db

        # DB_NAME is evaluated on import, so patch the module for an isolated DB.
        self.db = db
        self.old_module_db_name = db.DB_NAME
        db.DB_NAME = self.db_path
        db.init_db()

        conn = db.get_connection()
        conn.execute("INSERT INTO users(telegram_id) VALUES (1), (2)")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.db.DB_NAME = self.old_module_db_name
        if self.old_db_name is None:
            os.environ.pop("DB_NAME", None)
        else:
            os.environ["DB_NAME"] = self.old_db_name
        self.temp_dir.cleanup()

    def test_external_telegram_identity_has_one_active_owner(self):
        conn = self.db.get_connection()
        conn.execute(
            "INSERT INTO user_telegram_sessions "
            "(telegram_id, external_telegram_id, encrypted_session, status) "
            "VALUES (1, 999, 'ciphertext', 'connected')"
        )
        conn.commit()

        with self.assertRaisesRegex(Exception, "UNIQUE constraint failed"):
            conn.execute(
                "INSERT INTO user_telegram_sessions "
                "(telegram_id, external_telegram_id, encrypted_session, status) "
                "VALUES (2, 999, 'ciphertext', 'connected')"
            )
        conn.close()

    def test_disconnected_owner_releases_external_identity(self):
        conn = self.db.get_connection()
        conn.execute(
            "INSERT INTO user_telegram_sessions "
            "(telegram_id, external_telegram_id, encrypted_session, status) "
            "VALUES (1, 999, 'ciphertext', 'disconnected')"
        )
        conn.execute(
            "INSERT INTO user_telegram_sessions "
            "(telegram_id, external_telegram_id, encrypted_session, status) "
            "VALUES (2, 999, 'ciphertext', 'connected')"
        )
        conn.commit()
        conn.close()

    def test_reconnect_required_session_is_not_active_but_retains_ownership(self):
        conn = self.db.get_connection()
        conn.execute(
            "INSERT INTO user_telegram_sessions "
            "(telegram_id, external_telegram_id, encrypted_session, status) "
            "VALUES (1, 999, 'ciphertext', 'connected')"
        )
        conn.commit()
        conn.close()

        self.assertTrue(self.db.mark_telegram_session_reconnect_required(1))
        self.assertFalse(self.db.mark_telegram_session_reconnect_required(1))

        conn = self.db.get_connection()
        status = conn.execute(
            "SELECT status FROM user_telegram_sessions WHERE telegram_id=1"
        ).fetchone()["status"]
        self.assertEqual(status, "reconnect_required")
        with self.assertRaisesRegex(Exception, "UNIQUE constraint failed"):
            conn.execute(
                "INSERT INTO user_telegram_sessions "
                "(telegram_id, external_telegram_id, encrypted_session, status) "
                "VALUES (2, 999, 'ciphertext', 'connected')"
            )
        conn.close()

    def test_reconnect_claim_can_only_be_replaced_by_the_same_external_account(self):
        conn = self.db.get_connection()
        conn.execute(
            "INSERT INTO user_telegram_sessions "
            "(telegram_id, external_telegram_id, encrypted_session, status) "
            "VALUES (1, 999, 'ciphertext', 'reconnect_required')"
        )
        conn.commit()

        self.db.verify_telegram_session_reconnect_identity(conn.cursor(), 1, 999)
        with self.assertRaisesRegex(ValueError, "reconnect identity"):
            self.db.verify_telegram_session_reconnect_identity(conn.cursor(), 1, 123)
        conn.close()


if __name__ == "__main__":
    unittest.main()
