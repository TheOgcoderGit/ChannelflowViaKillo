"""Regression tests for the P0 atomic daily forwarding quota."""

import concurrent.futures
import os
import tempfile
import unittest


class DailyQuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "quota.db")

        import database.db as db
        import services.plan_service as plan_service

        self.db = db
        self.plan_service = plan_service
        self.old_db_name = db.DB_NAME
        self.old_get_entitlements = plan_service.get_entitlements
        db.DB_NAME = self.db_path
        db.init_db()

        conn = db.get_connection()
        conn.execute("INSERT INTO users(telegram_id) VALUES (1)")
        conn.execute("INSERT INTO projects(id, user_id, name) VALUES (1, 1, 'Quota test')")
        conn.commit()
        conn.close()

        plan_service.get_entitlements = lambda _user_id: {
            "per_project_daily_forward_limit": 1,
        }

    def tearDown(self):
        self.plan_service.get_entitlements = self.old_get_entitlements
        self.db.DB_NAME = self.old_db_name
        self.temp_dir.cleanup()

    def test_only_one_concurrent_reservation_can_consume_the_last_unit(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda _unused: self.plan_service.reserve_daily_forward(1, 1),
                    range(2),
                )
            )

        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 1)

        conn = self.db.get_connection()
        count = conn.execute(
            "SELECT forward_count FROM daily_usage WHERE project_id=1 AND usage_date=date('now')"
        ).fetchone()["forward_count"]
        conn.close()
        self.assertEqual(count, 1)

    def test_failed_dispatch_can_return_its_reservation(self):
        self.assertTrue(self.plan_service.reserve_daily_forward(1, 1))
        self.plan_service.release_daily_forward(1)
        self.assertTrue(self.plan_service.reserve_daily_forward(1, 1))


if __name__ == "__main__":
    unittest.main()
