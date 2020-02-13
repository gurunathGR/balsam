# flake8: noqa
"""LiveServer + Balsam Client API-driven tests"""

from django.test import LiveServerTestCase
from rest_framework import status


class BalsamClientFT(LiveServerTestCase):
    def setUp(self):
        return
        staging_server = os.environ.get("BALSAM_STAGING_SERVER")
        if staging_server:
            self.live_server_url = "http://" + staging_server

    @staticmethod
    def generate_password(len=20):
        atoz = range(ord("a"), ord("z") + 1)
        return "".join(map(chr, random.choices(atoz, k=len)))

    def register_client(self, username):
        password = self.generate_password()
        resp = api.BalsamClient.register(username, password, url=self.live_server_url)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        client = api.BalsamClient(
            self.live_server_url, username=username, password=password,
        )
        self.assertIn("apps", client._document)
        return client


class ClientAuthTests(BalsamClientFT):
    def test_client_can_register_and_login(self):
        return
        bob = self.register_client("Bob")
        userlist = bob.users()
        self.assertEqual(len(userlist), 1)
        me = userlist[0]
        self.assertEqual(me["username"], "Bob")
        self.assertEqual(me["owned_sites"], [])
        self.assertEqual(bob.sites(), [])