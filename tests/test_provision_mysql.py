import unittest

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


if __name__ == "__main__":
    unittest.main()
