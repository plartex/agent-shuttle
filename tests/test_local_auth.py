"""Security boundary tests for the loopback A2A server."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from agent_shuttle.a2a_server import make_app
from agent_shuttle.client import BridgeClient
from agent_shuttle.local_auth import (LocalCredential, PeerAuthenticationError, _windows_acl,
                                      read_local_credential)


class LocalAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_middleware_and_public_card(self):
        app = make_app("test", object(), "http://127.0.0.1:8765")
        credential = app.state.local_credential
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=credential.origin) as client:
            card = await asyncio.wait_for(client.get("/.well-known/agent-card.json"), timeout=3)
            self.assertEqual(card.status_code, 200)
            self.assertIn("agentShuttleBearer", card.json()["securitySchemes"])
            self.assertNotIn("workspace", card.text)
            self.assertEqual((await client.get("/bridge/identity")).status_code, 401)
            self.assertEqual((await client.post("/", json={})).status_code, 401)
            self.assertEqual((await client.get("/bridge/identity", headers={
                "Authorization": "Bearer wrong"})).status_code, 401)
            valid = {"Authorization": "Bearer " + credential.token}
            self.assertEqual((await client.get("/bridge/identity", headers=valid)).status_code, 200)
            self.assertEqual((await client.get("/bridge/identity", headers={
                **valid, "Origin": "null"})).status_code, 403)
            self.assertEqual((await client.get("/bridge/identity", headers={
                **valid, "Host": "127.0.0.1:8766"})).status_code, 421)
            nonce = "A" * 43
            proof = (await client.get("/bridge/proof", params={"nonce": nonce})).json()
            self.assertEqual(proof["signature"], credential.signature(nonce))

    async def test_record_rotation_and_ownership(self):
        with tempfile.TemporaryDirectory() as parent:
            with patch("agent_shuttle.local_auth._directory", return_value=Path(parent) / "run"):
                first = LocalCredential.fresh("http://127.0.0.1:8765")
                second = LocalCredential.fresh(first.origin)
                first.publish()
                self.assertEqual(read_local_credential(first.origin), first)
                second.publish()
                first.remove_if_owned()
                self.assertEqual(read_local_credential(first.origin), second)
                second.remove_if_owned()
                self.assertIsNone(read_local_credential(first.origin))

    async def test_replayed_instance_id_does_not_receive_bearer(self):
        with tempfile.TemporaryDirectory() as parent:
            with patch("agent_shuttle.local_auth._directory", return_value=Path(parent) / "run"):
                record = LocalCredential.fresh("http://127.0.0.1:8765")
                record.publish()
                seen_headers = []

                def fake_peer(request):
                    seen_headers.append(request.headers.get("Authorization"))
                    return httpx.Response(200, json={"origin": record.origin,
                        "instance_id": record.instance_id, "signature": "0" * 64})

                real_client = httpx.AsyncClient
                transport = httpx.MockTransport(fake_peer)
                with patch("agent_shuttle.client.httpx.AsyncClient",
                           side_effect=lambda **kw: real_client(transport=transport, **kw)):
                    with self.assertRaises(PeerAuthenticationError):
                        await BridgeClient()._http(record.origin)
                    with self.assertRaises(PeerAuthenticationError):
                        await BridgeClient(credentials={record.origin: record.token})._http(record.origin)
                self.assertEqual(seen_headers, [None, None])

    async def test_explicit_token_is_bound_to_origin(self):
        client = BridgeClient(credentials={"https://peer.example:443": "external-secret"})
        async with await client._http("https://peer.example:443") as exact:
            self.assertEqual(exact.headers["Authorization"], "Bearer external-secret")
        async with await client._http("https://other.example:443") as other:
            self.assertNotIn("Authorization", other.headers)

    async def test_unsafe_record_permissions_fail_closed(self):
        with tempfile.TemporaryDirectory() as parent:
            directory = Path(parent) / "run"
            with patch("agent_shuttle.local_auth._directory", return_value=directory):
                credential = LocalCredential.fresh("http://127.0.0.1:8765")
                credential.publish()
                path = directory / "8765.json"
                try:
                    if os.name == "nt":
                        import win32security
                        descriptor = win32security.GetFileSecurity(str(path),
                            win32security.DACL_SECURITY_INFORMATION)
                        acl = descriptor.GetSecurityDescriptorDacl()
                        everyone = win32security.CreateWellKnownSid(
                            win32security.WinWorldSid, None)
                        acl.AddAccessAllowedAce(win32security.ACL_REVISION, 1, everyone)
                        descriptor.SetSecurityDescriptorDacl(1, acl, 0)
                        win32security.SetFileSecurity(str(path),
                            win32security.DACL_SECURITY_INFORMATION, descriptor)
                    else:
                        path.chmod(0o644)
                    with self.assertRaises(PeerAuthenticationError):
                        read_local_credential(credential.origin)
                finally:
                    if os.name == "nt":
                        _windows_acl(path, set_acl=True)
                    else:
                        path.chmod(0o600)
                    credential.remove_if_owned()

    async def test_parallel_ports_are_independent(self):
        with tempfile.TemporaryDirectory() as parent:
            with patch("agent_shuttle.local_auth._directory", return_value=Path(parent) / "run"):
                first = LocalCredential.fresh("http://127.0.0.1:8765")
                second = LocalCredential.fresh("http://127.0.0.1:8766")
                first.publish()
                second.publish()
                first.remove_if_owned()
                self.assertEqual(read_local_credential(second.origin), second)
                second.remove_if_owned()
