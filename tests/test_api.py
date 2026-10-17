"""REST API (/api/v1), Go's /api/, /ppv/ and /data_images/."""

from __future__ import annotations

import io
import json
import zipfile

from webfixture import AppTestCase

from threadfin.access import COOKIE
from threadfin.api import INVALID_COMMAND, LOGIN_INCORRECT
from threadfin.auth import LEVEL_API, LEVEL_WEB

JSON = {"content-type": "application/json"}


class Basics(AppTestCase):
    def test_status(self) -> None:
        info = self.client.get("/api/v1/status").json()
        self.assertEqual((info["streams.all"], info["streams.active"]), (3, 3))
        self.assertEqual(info["url.m3u"], "http://testserver/m3u/threadfin.m3u")
        self.assertEqual(info["providers"], {"m3u": 1, "xmltv": 1})
        self.assertTrue(info["report"]["ok"])

    def test_csrf_needs_header_or_json(self) -> None:
        response = self.client.post("/api/v1/update", data={"x": "1"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.post("/api/v1/build", headers={"X-Requested-With": "x"}).status_code, 200)
        self.assertEqual(self.client.post("/api/v1/build", content=b"{}", headers=JSON).status_code, 200)

    def test_unknown_routes_and_methods(self) -> None:
        self.assertEqual(self.client.get("/api/v1/providers/dvb").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/providers/hdhr").status_code, 404)  # not supported
        self.assertEqual(self.client.get("/api/v1/channels/nope").status_code, 404)
        self.assertEqual(self.client.put("/api/v1/settings", json={}).status_code, 405)

    def test_invalid_json(self) -> None:
        response = self.client.patch("/api/v1/settings", content=b"{nope", headers=JSON)
        self.assertEqual(response.status_code, 400)
        self.assertIn("invalid JSON", response.json()["error"])
        self.assertEqual(self.client.patch("/api/v1/settings", json=[1]).status_code, 400)


class Settings(AppTestCase):
    def test_patch(self) -> None:
        response = self.client.patch("/api/v1/settings", json={"tuner": 4, "update": ["0300", " 12 30"]})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["changed"], ["tuner", "update"])
        self.assertEqual(body["settings"]["update"], ["0300", "1230"])
        saved = json.loads(self.system.file_settings.read_text())
        self.assertEqual((saved["tuner"], saved["update"]), (4, ["0300", "1230"]))

    def test_refused_changes_nothing(self) -> None:
        for change in ({"update": ["2400"]}, {"port": "1"}, {"tuner": "4"}, {"epgSource": "XML"}):
            with self.subTest(change):
                response = self.client.patch("/api/v1/settings", json={"tuner": 9, **change})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.service.settings.tuner, 1)

    def test_epg_source_rebuilds(self) -> None:
        self.client.patch("/api/v1/settings", json={"epgSource": "PMS"})
        self.assertEqual(self.client.get("/api/v1/status").json()["url.m3u"], "")
        self.assertEqual(len(self.client.get("/lineup.json").json()), 3)

    def test_guide_settings_rebuild(self) -> None:
        report = self.service.report
        self.client.patch("/api/v1/settings", json={"tuner": 2})
        self.assertIs(self.service.report, report)
        self.client.patch("/api/v1/settings", json={"xepg.replace.missing.images": False})
        self.assertIsNot(self.service.report, report)

    def test_settings_object_stays_the_same(self) -> None:
        settings = self.service.settings
        self.client.patch("/api/v1/settings", json={"tuner": 2})
        self.assertIs(self.service.settings, settings)
        self.assertIs(self.service.manager._settings, settings)


class Providers(AppTestCase):
    def test_crud(self) -> None:
        response = self.client.post("/api/v1/providers/m3u", json={"file.source": f"{self.url}/two.m3u", "name": "Two"})
        self.assertEqual(response.status_code, 201, response.text)
        new_id = response.json()["id"]
        self.assertEqual(response.json()["streams"], 3)
        listing = self.client.get("/api/v1/providers/m3u").json()
        self.assertEqual(len(listing), 2)
        response = self.client.patch(f"/api/v1/providers/m3u/{new_id}", json={"name": "Renamed", "tuner": 2})
        self.assertEqual((response.json()["name"], response.json()["tuner"]), ("Renamed", 2))
        response = self.client.post(f"/api/v1/providers/m3u/{new_id}/refresh", headers={"X-Requested-With": "x"})
        self.assertEqual((response.status_code, response.json()["ok"]), (200, True))
        self.assertEqual(self.client.delete(f"/api/v1/providers/m3u/{new_id}").status_code, 204)
        self.assertEqual(self.client.delete(f"/api/v1/providers/m3u/{new_id}").status_code, 404)
        self.assertFalse((self.system.folder_data / f"{new_id}.m3u").exists())

    def test_failed_download_is_not_kept(self) -> None:
        response = self.client.post("/api/v1/providers/m3u", json={"file.source": f"{self.url}/missing.m3u"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.service.manager.records("m3u")), 1)

    def test_validation(self) -> None:
        provider_id = next(iter(self.service.manager.records("m3u")))
        for body in ({"tuner": 0}, {"file.m3u.path": "/etc/passwd"}, {"file.source": ""}):
            with self.subTest(body):
                self.assertEqual(self.client.patch(f"/api/v1/providers/m3u/{provider_id}", json=body).status_code, 400)
        self.assertEqual(self.client.post("/api/v1/providers/m3u", json={"name": "no source"}).status_code, 400)
        self.assertEqual(self.client.patch("/api/v1/providers/m3u/nope", json={"name": "x"}).status_code, 404)


class Filters(AppTestCase):
    def test_crud_and_effect(self) -> None:
        response = self.client.post("/api/v1/filters", json={"type": "group-title", "filter": "News", "name": "News"})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["id"], "0")
        self.assertEqual(self.client.get("/api/v1/status").json()["streams.active"], 2)
        response = self.client.patch("/api/v1/filters/0", json={"exclude": "Gamma"})
        self.assertEqual(response.json()["exclude"], "Gamma")
        self.assertEqual(self.client.get("/api/v1/status").json()["streams.active"], 1)
        self.assertEqual(self.client.patch("/api/v1/filters/0", json={"filter": ""}).status_code, 400)
        self.assertEqual(self.client.delete("/api/v1/filters/0").status_code, 204)
        self.assertEqual(self.client.delete("/api/v1/filters/0").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/filters").json(), {})

    def test_groups(self) -> None:
        self.assertEqual(self.client.get("/api/v1/groups").json(),
                         [{"name": "News", "count": 2}, {"name": "Sports", "count": 1}])  # fmt: skip


class Channels(AppTestCase):
    def test_list_and_filter(self) -> None:
        channels = self.client.get("/api/v1/channels").json()
        self.assertEqual(sorted(c["name"] for c in channels), ["Alpha", "Beta", "Gamma"])
        self.assertEqual(len(self.client.get("/api/v1/channels?group=News").json()), 2)
        active = self.client.get("/api/v1/channels?active=true").json()
        self.assertTrue(all(c["x-active"] for c in active))

    def test_patch(self) -> None:
        channel_id = self.channel_ids()["Beta"]
        response = self.client.patch(f"/api/v1/channels/{channel_id}", json={"x-name": "Beta HD", "x-channelID": "42"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["x-name"], response.json()["x-channelID"]), ("Beta HD", "42"))
        playlist = self.client.get("/m3u/threadfin.m3u").text
        self.assertIn('tvg-chno="42"', playlist)
        self.assertIn("Beta HD", playlist)
        saved = json.loads(self.system.file_xepg.read_text())
        self.assertEqual(saved[channel_id]["x-name"], "Beta HD")

    def test_patch_validation(self) -> None:
        channel_id = self.channel_ids()["Beta"]
        for body in ({"x-channelID": "x"}, {"url": "http://evil"}, {"x-xmltv-file": "nope.xml"},
                     {"x-mapping": "zzz"}, {"x-active": True, "x-xmltv-file": "-"}):  # fmt: skip
            with self.subTest(body):
                self.assertEqual(self.client.patch(f"/api/v1/channels/{channel_id}", json=body).status_code, 400)
        self.assertEqual(self.client.patch("/api/v1/channels/nope", json={"x-name": "x"}).status_code, 404)

    def test_remap_to_dummy(self) -> None:
        channel_id = self.channel_ids()["Beta"]
        body = {"x-xmltv-file": "Threadfin Dummy", "x-mapping": "60_Minutes", "x-active": True}
        self.assertEqual(self.client.patch(f"/api/v1/channels/{channel_id}", json=body).status_code, 200)
        self.assertEqual(self.service.channels[channel_id].x_mapping, "60_Minutes")

    def test_bulk(self) -> None:
        ids = list(self.channel_ids().values())
        response = self.client.post("/api/v1/channels/bulk", json={"ids": ids, "changes": {"x-channels-start": "500"}})
        self.assertEqual(response.json(), {"changed": 3})
        self.assertEqual(sorted(c.x_channel_id for c in self.service.channels.values()), ["500", "501", "502"])
        response = self.client.post("/api/v1/channels/bulk", json={"ids": ids, "changes": {"x-active": False}})
        self.assertFalse(any(c.x_active for c in self.service.channels.values()))
        self.assertEqual(self.client.get("/m3u/threadfin.m3u").text.count("#EXTINF"), 0)
        for body in ({"ids": ids, "changes": {"x-name": "same"}}, {"ids": ["nope"], "changes": {"x-active": True}},
                     {"ids": "x", "changes": {}}):  # fmt: skip
            with self.subTest(body):
                self.assertEqual(self.client.post("/api/v1/channels/bulk", json=body).status_code, 400)

    def test_patch_backup_channel_resolves_after_a_rebuild(self) -> None:
        channel_id = self.channel_ids()["Beta"]
        response = self.client.patch(f"/api/v1/channels/{channel_id}", json={"x-backup-channel-1": "Gamma"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.channels[channel_id].x_backup_channel_1, "Gamma")
        info = next(i for i in self.service.registry.entries.values() if i.name == "Beta")
        assert info.backup_channel_1 is not None
        self.assertEqual(info.backup_channel_1.url, "http://s/3")

    def test_unmatched_backup_is_reported(self) -> None:
        channel_id = self.channel_ids()["Beta"]
        self.client.patch(f"/api/v1/channels/{channel_id}", json={"x-backup-channel-2": "Nowhere"})
        self.assertTrue(any("Beta" in w and "Nowhere" in w for w in self.service.report.warnings))
        self.client.patch(f"/api/v1/channels/{channel_id}", json={"x-backup-channel-2": "Gamma"})
        self.assertFalse(any("Nowhere" in w for w in self.service.report.warnings))

    def test_xmltv_channels(self) -> None:
        files = self.client.get("/api/v1/xmltv").json()
        guide = next(name for name in files if name != "Threadfin Dummy")
        self.assertEqual([c["name"] for c in files[guide]], ["Alpha TV", "Beta TV", "Gamma TV"])

    def test_probe_rejects_unprobeable_url(self) -> None:
        channel_id = self.channel_ids()["Alpha"]
        self.service.channels[channel_id].url = "file:///etc/passwd"
        response = self.client.post(f"/api/v1/channels/{channel_id}/probe", headers={"X-Requested-With": "x"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get(f"/api/v1/channels/{channel_id}/probes").json(), [])


class Users(AppTestCase):
    def test_crud(self) -> None:
        response = self.client.post("/api/v1/users", json={"username": "bob", "password": "pw", LEVEL_WEB: True})
        self.assertEqual(response.status_code, 201, response.text)
        user_id = response.json()["id"]
        self.assertNotIn("password", response.json())
        self.assertEqual(self.client.post("/api/v1/users", json={"username": "bob", "password": "x"}).status_code, 400)
        response = self.client.patch(f"/api/v1/users/{user_id}", json={LEVEL_API: True, "password": "new"})
        self.assertIs(response.json()[LEVEL_API], True)
        self.assertIs(response.json()[LEVEL_WEB], True)  # kept
        self.service.auth.authenticate("bob", "new")
        self.assertEqual(self.client.patch(f"/api/v1/users/{user_id}", json={LEVEL_API: "yes"}).status_code, 400)
        self.assertEqual(self.client.delete(f"/api/v1/users/{user_id}").status_code, 204)
        self.assertEqual(self.client.get("/api/v1/users").json(), [])

    def test_last_user_stays_while_login_is_required(self) -> None:
        self.service.auth.create_first_user("admin", "pw")
        user_id = next(iter(self.service.auth.users))
        self.enable_auth()
        token = self.client.post("/api/v1/login", json={"username": "admin", "password": "pw"}).json()["token"]
        response = self.client.delete(f"/api/v1/users/{user_id}", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 400)


class Login(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.service.auth.create_first_user("admin", "pw")
        self.service.auth.save_new_user({"username": "viewer", "password": "pw", LEVEL_WEB: False})
        self.enable_auth()

    def test_needs_a_session(self) -> None:
        self.assertEqual(self.client.get("/api/v1/status").status_code, 401)
        self.assertEqual(
            self.client.post("/api/v1/login", json={"username": "admin", "password": "no"}).status_code, 401
        )
        self.assertEqual(
            self.client.post("/api/v1/login", json={"username": "viewer", "password": "pw"}).status_code, 401
        )
        self.assertEqual(self.client.post("/api/v1/login", content=b"nope", headers=JSON).status_code, 400)

    def test_bearer_and_cookie(self) -> None:
        token = self.client.post("/api/v1/login", json={"username": "admin", "password": "pw"}).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        self.assertEqual(self.client.get("/api/v1/status", headers=auth).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/status", headers=auth).status_code, 200)  # not rotated
        self.client.cookies.set(COOKIE, token)
        self.assertEqual(self.client.get("/api/v1/status").status_code, 200)
        self.client.cookies.clear()
        self.assertEqual(self.client.post("/api/v1/logout", headers=auth).status_code, 204)
        self.assertEqual(self.client.get("/api/v1/status", headers=auth).status_code, 401)

    def test_losing_web_access_ends_the_session(self) -> None:
        token = self.client.post("/api/v1/login", json={"username": "admin", "password": "pw"}).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        user_id = self.service.auth.session(token)
        self.service.auth.write_user_data(user_id, {**self.service.auth.read_user_data(user_id), LEVEL_WEB: False})
        self.assertEqual(self.client.get("/api/v1/status", headers=auth).status_code, 401)

    def test_cannot_lock_yourself_out(self) -> None:
        token = self.client.post("/api/v1/login", json={"username": "admin", "password": "pw"}).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        me = self.service.auth.session(token)
        self.assertEqual(self.client.delete(f"/api/v1/users/{me}", headers=auth).status_code, 400)
        self.assertEqual(
            self.client.patch(f"/api/v1/users/{me}", json={LEVEL_WEB: False}, headers=auth).status_code, 400
        )
        self.assertIn(me, self.service.auth.users)


class Backup(AppTestCase):
    def test_download_and_restore(self) -> None:
        response = self.client.get("/api/v1/backup")
        self.assertEqual(response.headers["content-type"], "application/zip")
        names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
        self.assertIn("settings.json", names)
        self.assertEqual(list(self.system.folder_temp.glob("threadfin_backup_*.zip")), [])  # deleted after download

        self.client.patch("/api/v1/settings", json={"tuner": 7})
        restored = self.client.post("/api/v1/restore", files={"file": ("b.zip", response.content, "application/zip")},
                                    headers={"X-Requested-With": "x"})  # fmt: skip
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json(), {"portChanged": False, "webURL": ""})
        self.assertEqual(self.service.settings.tuner, 1)

    def test_restore_rejects_junk(self) -> None:
        response = self.client.post("/api/v1/restore", content=b"not a zip",
                                    headers={"content-type": "application/zip", "X-Requested-With": "x"})  # fmt: skip
        self.assertEqual(response.status_code, 400)
        self.assertIn("not a backup archive", response.json()["error"])


class Logos(AppTestCase):
    def test_upload_and_serve(self) -> None:
        response = self.client.post("/api/v1/logos", files={"file": ("../../My Logo.png", b"\x89PNG", "image/png")},
                                   headers={"X-Requested-With": "x"})  # fmt: skip
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["url"], "http://testserver/data_images/My_Logo.png")
        self.assertEqual(self.client.get("/data_images/My%20Logo.png").status_code, 404)  # only the stored name
        served = self.client.get("/data_images/My_Logo.png")
        self.assertEqual(
            (served.status_code, served.content, served.headers["content-type"]), (200, b"\x89PNG", "image/png")
        )
        for path in ("/data_images/..%2Fsettings.json", "/data_images/missing.png", "/data_images/x.exe"):
            with self.subTest(path):
                self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(
            self.client.post("/api/v1/logos", files={"file": ("a.png", b"x", "image/png")}).status_code, 403
        )
        bad = self.client.post("/api/v1/logos", files={"file": ("x.exe", b"MZ", "application/octet-stream")},
                               headers={"X-Requested-With": "x"})  # fmt: skip
        self.assertEqual(bad.status_code, 400)


class GoAPI(AppTestCase):
    def call(self, body: object) -> object:
        return self.client.post("/api/", content=json.dumps(body).encode()).json()

    def test_disabled(self) -> None:
        response = self.client.post("/api/", content=b'{"cmd": "status"}')
        self.assertEqual((response.status_code, response.text), (423, "Locked [423]\n"))

    def test_status_and_errors(self) -> None:
        self.service.settings.api = True
        self.assertEqual(self.client.get("/api/").status_code, 404)
        self.assertEqual(self.client.post("/api/", content=b"nope").status_code, 400)
        status = self.call({"cmd": "status"})
        self.assertEqual(status["status"], True)
        self.assertEqual((status["streams.all"], status["epg.source"]), (3, "XEPG"))
        self.assertEqual(status["url.m3u"], "http://testserver/m3u/threadfin.m3u")
        self.assertEqual(status["version.api"], "1.2.40")
        self.assertEqual(self.call({"cmd": "nope"}), {"err": INVALID_COMMAND, "status": False})
        for cmd in ("update.m3u", "update.xmltv", "update.xepg"):
            with self.subTest(cmd):
                self.assertEqual(self.call({"cmd": cmd}), {"status": True})
        self.assertEqual(self.call({"cmd": "update.hdhr"}), {"err": INVALID_COMMAND, "status": False})

    def test_token_flow(self) -> None:
        self.service.settings.api = True
        self.service.settings.authentication_web = self.service.settings.authentication_api = True
        self.service.auth.save_new_user({"username": "script", "password": "pw", LEVEL_API: True})
        self.service.auth.save_new_user({"username": "nope", "password": "pw", LEVEL_API: False})
        self.assertEqual(self.call({"cmd": "status"}), {"err": LOGIN_INCORRECT, "status": False})
        self.assertEqual(self.call({"cmd": "login", "username": "nope", "password": "pw"})["status"], False)
        token = self.call({"cmd": "login", "username": "script", "password": "pw"})["token"]
        status = self.call({"cmd": "status", "token": token})
        self.assertEqual(status["status"], True)
        self.assertNotEqual(status["token"], token)  # replaced in every answer, as in Go
        self.assertEqual(self.call({"cmd": "status", "token": token})["status"], False)
        self.assertEqual(self.call({"cmd": "status", "token": status["token"]})["status"], True)


class GoAPIEmpty(AppTestCase):
    PROVIDERS = False

    def test_status_leaves_out_empty_values(self) -> None:
        self.service.settings.api = True
        status = self.client.post("/api/", content=b'{"cmd": "status"}').json()
        self.assertNotIn("streams.all", status)  # 0: omitempty, as in Go
        self.assertEqual(status["status"], True)


class PPV(AppTestCase):
    def test_enable_disable(self) -> None:
        channel_id = self.channel_ids()["Beta"]
        self.client.patch(f"/api/v1/channels/{channel_id}",
                          json={"x-xmltv-file": "Threadfin Dummy", "x-mapping": "PPV", "x-active": False})  # fmt: skip
        self.assertEqual(self.client.get("/ppv/enable").status_code, 200)
        self.assertTrue(self.service.channels[channel_id].x_active)
        self.client.post("/ppv/disable")
        self.assertFalse(self.service.channels[channel_id].x_active)
        alpha = self.service.channels[self.channel_ids()["Alpha"]]
        self.assertTrue(alpha.x_active)  # not a PPV channel
