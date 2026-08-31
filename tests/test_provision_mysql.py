import unittest
from unittest import mock

import provision_mysql as pm


class FakeApi:
    def __init__(self):
        self.pages = []

    def get(self, path):
        self.pages.append(path)
        if "page=1" in path:
            return {"users": [{"name": f"u{i}"} for i in range(200)]}
        return {"users": [{"name": "mail_watch"}]}


class ProvisionTests(unittest.TestCase):
    def test_rejects_admin_user_before_api_access(self):
        args = pm.build_parser().parse_args(
            ["--cluster", "db-mysql-fra1-42798", "--user", "doadmin"]
        )
        with self.assertRaises(SystemExit) as caught:
            pm.provision(args)
        self.assertIn("fail-closed", str(caught.exception))

    def test_rejects_system_database_before_api_access(self):
        args = pm.build_parser().parse_args(
            ["--cluster", "db-mysql-fra1-42798", "--database", "mysql"]
        )
        with self.assertRaises(SystemExit):
            pm.provision(args)

    def test_paginates_digitalocean_collections(self):
        api = FakeApi()
        users = pm._all(api, "/databases/id/users", "users")
        self.assertEqual(len(users), 201)
        self.assertTrue(any("page=2" in path for path in api.pages))

    @mock.patch("provision_mysql._revoke_current_grants")
    def test_migration_failure_still_restores_runtime_dml(self, revoke):
        cursor = mock.MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.__exit__.return_value = False
        admin = mock.MagicMock()
        admin.cursor.return_value = cursor

        with self.assertRaisesRegex(RuntimeError, "migration failed"):
            with pm._temporary_migration_grants(admin, "'mail_watch'@'%'", "`agent_mail`"):
                raise RuntimeError("migration failed")

        self.assertEqual(revoke.call_count, 2)
        grants = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(any("CREATE, ALTER, INDEX" in sql for sql in grants))
        self.assertTrue(any(
            sql == "GRANT SELECT, INSERT, UPDATE, DELETE ON `agent_mail`.* TO 'mail_watch'@'%'"
            for sql in grants
        ))


if __name__ == "__main__":
    unittest.main()
