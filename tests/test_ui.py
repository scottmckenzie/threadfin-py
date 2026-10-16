"""Web UI (/web/): pages, dialogs, login. htmx requests are simulated with the HX-Request header."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

from webfixture import AppTestCase

from threadfin.access import COOKIE
from threadfin.auth import LEVEL_API, LEVEL_WEB

HX = {"HX-Request": "true"}


def triggers(response) -> dict:  # type: ignore[no-untyped-def]
    return json.loads(response.headers.get("hx-trigger", "{}"))


class Pages(AppTestCase):
    PAGES = ("/web/", "/web/playlists", "/web/xmltv", "/web/filters", "/web/mapping", "/web/settings", "/web/log")

    def test_pages_render(self) -> None:
        for path in self.PAGES:
            with self.subTest(path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn('<script src="/web/static/htmx-2.0.4.min.js"', response.text)
        self.assertEqual(self.client.get("/web", follow_redirects=False).status_code, 307)

    def test_menu_follows_settings(self) -> None:
        page = self.client.get("/web/").text
        self.assertIn('href="/web/mapping"', page)
        self.assertNotIn('href="/web/users"', page)  # only with web authentication
        self.service.settings.epg_source = "PMS"
        page = self.client.get("/web/").text
        self.assertNotIn('href="/web/mapping"', page)

    def test_status_panel(self) -> None:
        panel = self.client.get("/web/status/panel").text
        self.assertIn("http://testserver/m3u/threadfin.m3u", panel)
        self.assertIn("<strong>3 / 3</strong>", panel)

    def test_csrf(self) -> None:
        self.assertEqual(self.client.post("/web/rebuild").status_code, 403)
        response = self.client.post("/web/rebuild", headers=HX)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Rebuilt: 3 channels", triggers(response)["toast"])

    def test_static_files_only(self) -> None:
        self.assertEqual(self.client.get("/web/static/../settings.json").status_code, 404)
        self.assertEqual(self.client.get("/web/nope").status_code, 404)

    def test_html_is_escaped(self) -> None:
        channel_id = self.channel_ids()["Alpha"]
        self.client.patch(f"/api/v1/channels/{channel_id}", json={"x-name": "<script>alert(1)</script>"})
        rows = self.client.get("/web/mapping/rows").text
        self.assertNotIn("<script>alert", rows)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rows)


class Providers(AppTestCase):
    def test_no_hdhomerun_sources(self) -> None:
        page = self.client.get("/web/playlists").text
        self.assertIn("/web/providers/m3u/new", page)
        self.assertNotIn("hdhr", page)
        self.assertNotIn("HDHomeRun", page)
        self.assertEqual(self.client.get("/web/providers/hdhr/new").status_code, 404)

    def test_add_edit_refresh_delete(self) -> None:
        form = self.client.get("/web/providers/m3u/new").text
        self.assertIn('name="file.source"', form)
        response = self.client.post("/web/providers/m3u", data={"name": "Two", "file.source": f"{self.url}/two.m3u",
                                                                 "tuner": "2"}, headers=HX)  # fmt: skip
        self.assertEqual(triggers(response), {"toast": "Added: 3 streams", "close-dialog": True, "refresh-list": True})
        records = self.service.manager.records("m3u")
        new_id = next(pid for pid, r in records.items() if r["name"] == "Two")
        self.assertEqual(records[new_id]["tuner"], 2)
        self.assertIn("Two", self.client.get("/web/providers/m3u/list").text)

        edit = self.client.get(f"/web/providers/m3u/{new_id}/edit").text
        self.assertIn('value="Two"', edit)
        self.client.post(f"/web/providers/m3u/{new_id}", data={"name": "Renamed", "file.source": f"{self.url}/two.m3u",
                                                                "tuner": "3"}, headers=HX)  # fmt: skip
        self.assertEqual((records[new_id]["name"], records[new_id]["tuner"]), ("Renamed", 3))

        response = self.client.post(f"/web/providers/m3u/{new_id}/refresh", headers=HX)
        self.assertEqual(triggers(response)["toast"], "Updated: 3 streams")
        self.client.post(f"/web/providers/m3u/{new_id}/delete", headers=HX)
        self.assertNotIn(new_id, records)
        self.assertEqual(self.client.get(f"/web/providers/m3u/{new_id}/edit").status_code, 404)

    def test_errors_keep_the_dialog_open(self) -> None:
        response = self.client.post("/web/providers/m3u", data={"name": "X", "file.source": f"{self.url}/missing.m3u",
                                                                 "tuner": "1"}, headers=HX)  # fmt: skip
        self.assertNotIn("hx-trigger", response.headers)
        self.assertIn('class="error"', response.text)
        self.assertIn('value="X"', response.text)  # what was typed is kept
        response = self.client.post(
            "/web/providers/m3u", data={"name": "X", "file.source": "", "tuner": "1"}, headers=HX
        )
        self.assertIn("file.source is required", response.text)


class Filters(AppTestCase):
    def test_group_filter(self) -> None:
        form = self.client.get("/web/filters/new?type=group-title").text
        self.assertIn("News (2)", form)
        response = self.client.post("/web/filters", data={"type": "group-title", "name": "News", "filter": "News",
                                                           "active": "on", "startingNumber": "1000"}, headers=HX)  # fmt: skip
        self.assertTrue(triggers(response)["close-dialog"])
        self.assertEqual(self.service.settings.filter["0"]["filter"], "News")
        self.assertIs(self.service.settings.filter["0"]["caseSensitive"], False)
        self.assertIn("News", self.client.get("/web/filters/list").text)
        self.assertEqual(len(self.service.stream_db.active), 2)
        self.client.post("/web/filters/0/delete", headers=HX)
        self.assertEqual(self.service.settings.filter, {})

    def test_empty_rule(self) -> None:
        response = self.client.post(
            "/web/filters", data={"type": "custom-filter", "name": "x", "filter": ""}, headers=HX
        )
        self.assertIn("Invalid filter rule", response.text)
        self.assertEqual(self.service.settings.filter, {})


class Mapping(AppTestCase):
    def test_every_filter_control_reloads_the_list(self) -> None:
        """htmx ``from:find <selector>`` listens on the first match only: name each control."""
        page = self.client.get("/web/mapping").text
        form = re.search(r'<form class="toolbar"[^>]*hx-trigger="([^"]+)">(.*?)</form>', page, re.DOTALL)
        assert form is not None
        trigger, body = form.groups()
        for tag, name in re.findall(r'<(input|select) name="([^"]+)"', body):
            with self.subTest(name):
                self.assertIn(f"from:find {tag}[name={name}]", trigger)

    def test_rows_and_search(self) -> None:
        rows = self.client.get("/web/mapping/rows").text
        self.assertEqual(rows.count('name="ids"'), 3)
        self.assertIn("Alpha TV", rows)  # guide channel name
        self.assertEqual(self.client.get("/web/mapping/rows?q=beta").text.count('name="ids"'), 1)
        self.assertEqual(self.client.get("/web/mapping/rows?group=News").text.count('name="ids"'), 2)
        self.assertEqual(self.client.get("/web/mapping/rows?state=inactive").text.count('name="ids"'), 0)
        self.client.patch(f"/api/v1/channels/{self.channel_ids()['Beta']}", json={"x-active": False})
        self.assertEqual(self.client.get("/web/mapping/rows").text.count('name="ids"'), 2)  # active by default
        self.assertEqual(self.client.get("/web/mapping/rows?state=inactive").text.count('name="ids"'), 1)
        self.assertEqual(self.client.get("/web/mapping/rows?state=all").text.count('name="ids"'), 3)

    def test_edit(self) -> None:
        channel_id = self.channel_ids()["Beta"]
        form = self.client.get(f"/web/mapping/{channel_id}/edit").text
        self.assertIn('<input type="hidden" name="x-mapping" value="b">', form)
        self.assertIn('value="Beta TV (b)"', form)  # the picker shows the chosen guide channel
        self.assertNotIn("Alpha TV", form)  # but no list of guide channels
        self.assertNotIn("x-backup-channel", form)  # backup channels are not supported
        data = {"x-active": "on", "x-channelID": "7", "x-name": "Beta HD", "x-xmltv-file": self.service.channels[channel_id].x_xmltv_file,
                "x-mapping": "b", "tvg-logo": "http://x/b.png"}  # fmt: skip
        response = self.client.post(f"/web/mapping/{channel_id}", data=data, headers=HX)
        self.assertEqual(triggers(response)["toast"], "Saved")
        channel = self.service.channels[channel_id]
        self.assertEqual((channel.x_name, channel.x_channel_id, channel.tvg_logo), ("Beta HD", "7", "http://x/b.png"))
        self.assertFalse(channel.x_update_channel_name)  # unchecked box

    def test_edit_error_keeps_input(self) -> None:
        channel_id = self.channel_ids()["Beta"]
        data = {"x-active": "on", "x-channelID": "seven", "x-name": "Typed name", "x-xmltv-file": "-", "x-mapping": "-"}
        response = self.client.post(f"/web/mapping/{channel_id}", data=data, headers=HX)
        self.assertIn("is not a channel number", response.text)
        self.assertIn('value="Typed name"', response.text)
        self.assertEqual(self.service.channels[channel_id].x_name, "Beta")

    def guide(self) -> str:
        return next(name for name in self.service.xmltv_mapping if name != "Threadfin Dummy")

    def matches(self, file: str, q: str) -> list[str]:
        html = self.client.get("/web/mapping/xmltv-search", params={"x-xmltv-file": file, "q": q}).text
        return re.findall(r'data-xmltv-id="([^"]*)"', html)

    def test_xmltv_search(self) -> None:
        guide = self.guide()
        self.assertEqual(self.matches(guide, ""), [])  # nothing until something is typed
        self.assertEqual(self.matches(guide, "  "), [])
        self.assertEqual(self.matches(guide, "beta"), ["b"])
        self.assertEqual(self.matches(guide, "TV"), ["a", "b", "c"])
        self.assertEqual(self.matches(guide, "c"), ["c"])  # an exact id first ...
        self.assertEqual(self.matches("Threadfin Dummy", "min")[:2], ["120_Minutes", "180_Minutes"])
        html = self.client.get("/web/mapping/xmltv-search", params={"x-xmltv-file": guide, "q": "zzz"}).text
        self.assertIn("No guide channel matches", html)
        self.assertEqual(self.matches("nope.xml", "a"), [])

    def test_xmltv_search_ranking_and_limit(self) -> None:
        guide = self.guide()
        channels = {f"id{i}": {"display-name": f"News {i}"} for i in range(80)}
        channels["news"] = {"display-name": "Weather"}
        channels["x"] = {"display-name": "Morning news"}
        self.service.xmltv_mapping[guide] = channels
        found = self.matches(guide, "news")
        self.assertEqual(found[0], "news")  # exact id
        self.assertEqual(len(found), 50)
        self.assertNotIn("x", found)  # a substring match ranks after the 80 prefix matches
        html = self.client.get("/web/mapping/xmltv-search", params={"x-xmltv-file": guide, "q": "news"}).text
        self.assertIn("The first 50 matches", html)

    def test_picker_preselects_the_streams_tvg_id(self) -> None:
        channel_id = self.channel_ids()["Gamma"]  # tvg-id "c"
        html = self.client.get(
            "/web/mapping/xmltv-picker", params={"x-xmltv-file": self.guide(), "channel": channel_id}
        ).text
        self.assertIn('name="x-mapping" value="c"', html)
        self.assertIn('value="Gamma TV (c)"', html)
        html = self.client.get(
            "/web/mapping/xmltv-picker", params={"x-xmltv-file": "Threadfin Dummy", "channel": channel_id}
        ).text
        self.assertIn('name="x-mapping" value="-"', html)
        self.assertIn('name="q" value=""', html)  # nothing chosen: an empty search box
        self.assertIn('data-dummy="true"', html)
        html = self.client.get("/web/mapping/xmltv-picker", params={"x-xmltv-file": "-", "channel": channel_id}).text
        self.assertIn("disabled", html)
        self.assertEqual(
            self.client.get("/web/mapping/xmltv-picker", params={"x-xmltv-file": "-", "channel": "nope"}).status_code,
            404,
        )

    def test_mapping_missing_from_the_guide_is_kept(self) -> None:
        channel_id = self.channel_ids()["Beta"]
        del self.service.xmltv_mapping[self.guide()]["b"]  # e.g. the provider's file was incomplete
        form = self.client.get(f"/web/mapping/{channel_id}/edit").text
        self.assertIn('<input type="hidden" name="x-mapping" value="b">', form)
        self.assertIn('value="b (not in this guide)"', form)

    def test_save_with_the_picker(self) -> None:
        channel_id = self.channel_ids()["Beta"]
        data = {"x-active": "on", "x-channelID": "7", "x-name": "Beta", "x-xmltv-file": "Threadfin Dummy",
                "x-mapping": "60_Minutes", "q": "60 Minutes"}  # fmt: skip
        response = self.client.post(f"/web/mapping/{channel_id}", data=data, headers=HX)
        self.assertEqual(triggers(response)["toast"], "Saved")
        self.assertEqual(self.service.channels[channel_id].x_mapping, "60_Minutes")

    def test_bulk(self) -> None:
        ids = list(self.channel_ids().values())
        response = self.client.post(
            "/web/mapping/bulk", data={"ids": ids, "action": "renumber", "value": "200"}, headers=HX
        )
        self.assertEqual(triggers(response)["toast"], "3 channels changed")
        self.assertEqual(sorted(c.x_channel_id for c in self.service.channels.values()), ["200", "201", "202"])
        self.client.post("/web/mapping/bulk", data={"ids": ids[:1], "action": "deactivate"}, headers=HX)
        self.assertEqual(sum(c.x_active for c in self.service.channels.values()), 2)
        response = self.client.post("/web/mapping/bulk", data={"action": "activate"}, headers=HX)
        self.assertEqual(triggers(response)["toast"], "Select channels first")
        response = self.client.post(
            "/web/mapping/bulk", data={"ids": ids, "action": "renumber", "value": "x"}, headers=HX
        )
        self.assertIn("not a channel number", triggers(response)["toast"])

    def test_logo_upload(self) -> None:
        response = self.client.post("/web/logo", data={"tvg-logo": "old"}, files={"logo": ("a b.png", b"\x89PNG", "image/png")},
                                    headers=HX)  # fmt: skip
        self.assertIn('value="http://testserver/data_images/a_b.png"', response.text)
        response = self.client.post(
            "/web/logo", data={"tvg-logo": "old"}, files={"logo": ("a.exe", b"MZ", "x/y")}, headers=HX
        )
        self.assertIn('value="old"', response.text)
        self.assertIn("logos must be one of", response.text)

    def test_probe_refuses_local_urls(self) -> None:
        channel_id = self.channel_ids()["Alpha"]
        self.service.channels[channel_id].url = "/etc/passwd"
        response = self.client.post(f"/web/mapping/{channel_id}/probe", headers=HX)
        self.assertIn("unsupported URL scheme", response.text)


class Settings(AppTestCase):
    def form(self, page: str | None = None, **changes: str) -> dict[str, str]:
        """The values the settings form shows (the saved page, or ``page``), with ``changes``."""
        page = html.unescape(page if page is not None else self.client.get("/web/settings").text)
        data = dict(re.findall(r'<input name="([^"]+)"(?: type="number")? value="([^"]*)"', page))
        data |= {name: "on" for name in re.findall(r'<input type="checkbox" name="([^"]+)" checked', page)}
        data |= dict(re.findall(r'<select name="([^"]+)">.*?<option value="([^"]*)" selected', page, re.DOTALL))
        return {**data, **changes}

    def test_save(self) -> None:
        response = self.client.post("/web/settings", data=self.form(tuner="3", update="0300, 1500"), headers=HX)
        self.assertIn("Saved", response.text)
        self.assertEqual((self.service.settings.tuner, self.service.settings.update), (3, ["0300", "1500"]))
        response = self.client.post("/web/settings", data=self.form(), headers=HX)
        self.assertIn("No changes", response.text)

    def test_unchecking(self) -> None:
        self.assertTrue(self.service.settings.files_update)
        data = self.form()
        del data["files.update"]
        self.client.post("/web/settings", data=data, headers=HX)
        self.assertFalse(self.service.settings.files_update)

    def test_errors(self) -> None:
        response = self.client.post("/web/settings", data=self.form(update="2500"), headers=HX)
        self.assertIn('class="error"', response.text)
        self.assertEqual(self.service.settings.update, ["0000"])
        response = self.client.post("/web/settings", data=self.form(tuner="many"), headers=HX)
        self.assertIn("is not a number", response.text)

    def test_a_failed_save_keeps_what_was_typed(self) -> None:
        """Restored settings with an ffmpeg path missing here: fixing the backup path first must not
        lose the fix when the save is refused."""
        new_backup = str(self.system.config / "elsewhere") + "/"
        Path(new_backup).mkdir()
        before = self.service.settings.to_json_dict()
        response = self.client.post("/web/settings", headers=HX, data=self.form(
            **{"backup.path": new_backup, "ffmpeg.path": "/missing/ffmpeg", "tuner": "seven"}))  # fmt: skip
        self.assertIn('class="error"', response.text)
        self.assertEqual(self.service.settings.to_json_dict(), before)  # nothing was saved
        shown = self.form(page=response.text)
        self.assertEqual(shown["backup.path"], new_backup)  # the form shows what was typed
        self.assertEqual(shown["ffmpeg.path"], "/missing/ffmpeg")
        self.assertEqual(shown["tuner"], "seven")
        # the user corrects the other fields in that form and saves again
        response = self.client.post("/web/settings", headers=HX, data={**shown, "ffmpeg.path": "", "tuner": "2"})
        self.assertIn("Saved", response.text)
        self.assertEqual((self.service.settings.backup_path, self.service.settings.tuner), (new_backup, 2))

    def test_a_failed_save_keeps_checkboxes_and_times(self) -> None:
        data = self.form(update="0300, 2500")
        del data["files.update"]  # unchecked
        response = self.client.post("/web/settings", headers=HX, data=data)
        self.assertIn('class="error"', response.text)
        shown = self.form(page=response.text)
        self.assertNotIn("files.update", shown)
        self.assertEqual(shown["update"], "0300,2500")
        self.assertTrue(self.service.settings.files_update)  # not saved

    def test_turning_on_login_reloads(self) -> None:
        response = self.client.post("/web/settings", data=self.form(**{"authentication.web": "on"}), headers=HX)
        self.assertEqual(response.headers.get("hx-refresh"), "true")
        self.assertEqual(self.client.get("/web/", follow_redirects=False).headers["location"], "/web/first-user")

    def test_backup_and_restore(self) -> None:
        archive = self.client.get("/web/backup")
        self.assertEqual(archive.headers["content-type"], "application/zip")
        self.client.patch("/api/v1/settings", json={"tuner": 5})
        response = self.client.post(
            "/web/restore", files={"archive": ("b.zip", archive.content, "application/zip")}, headers=HX
        )
        self.assertIn("Backup restored", response.text)
        self.assertEqual(self.service.settings.tuner, 1)
        response = self.client.post(
            "/web/restore", files={"archive": ("b.zip", b"junk", "application/zip")}, headers=HX
        )
        self.assertIn("Restore failed: not a backup archive", response.text)


class Log(AppTestCase):
    def test_entries_and_clear(self) -> None:
        self.client.patch("/api/v1/settings", json={"tuner": 2})
        self.assertIn("settings saved: tuner", self.client.get("/web/log/entries").text)
        self.client.post("/web/log/clear", headers=HX)
        self.assertIn("Nothing logged yet", self.client.get("/web/log/entries").text)


class Login(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enable_auth()

    def login(self, username: str = "admin", password: str = "pw"):  # type: ignore[no-untyped-def]
        return self.client.post("/web/login", data={"username": username, "password": password}, follow_redirects=False)

    def test_first_user_then_login(self) -> None:
        self.assertEqual(self.client.get("/web/", follow_redirects=False).headers["location"], "/web/first-user")
        self.assertIn("Create the first user", self.client.get("/web/first-user").text)
        response = self.client.post("/web/first-user", data={"username": "admin", "password": "pw", "confirm": "nope"})
        self.assertIn("The passwords do not match", response.text)
        response = self.client.post("/web/first-user", data={"username": "admin", "password": "pw", "confirm": "pw"},
                                    follow_redirects=False)  # fmt: skip
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/web/"))
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertEqual(self.client.get("/web/").status_code, 200)
        self.assertIn('href="/web/users"', self.client.get("/web/").text)
        # The page is gone once a user exists.
        self.assertEqual(self.client.get("/web/first-user", follow_redirects=False).headers["location"], "/web/")
        self.client.post("/web/first-user", data={"username": "evil", "password": "x", "confirm": "x"})
        self.assertEqual(len(self.service.auth.users), 1)

    def test_login_logout(self) -> None:
        self.service.auth.create_first_user("admin", "pw")
        self.assertEqual(self.client.get("/web/", follow_redirects=False).headers["location"], "/web/login")
        response = self.client.get("/web/mapping/rows", headers=HX)
        self.assertEqual((response.status_code, response.headers["hx-redirect"]), (401, "/web/login"))
        response = self.login(password="wrong")
        self.assertEqual(response.status_code, 401)
        self.assertIn("User authentication failed", response.text)
        self.assertEqual(self.login().status_code, 303)
        self.assertEqual(self.client.get("/web/mapping").status_code, 200)
        response = self.client.post("/web/logout", headers=HX)
        self.assertEqual(response.headers["hx-redirect"], "/web/login")
        self.assertNotIn(COOKIE, self.client.cookies)
        self.assertEqual(self.client.get("/web/", follow_redirects=False).status_code, 303)

    def test_user_without_web_level(self) -> None:
        self.service.auth.create_first_user("admin", "pw")
        self.service.auth.save_new_user({"username": "plex", "password": "pw", LEVEL_WEB: False})
        self.assertEqual(self.login("plex").status_code, 401)

    def test_login_page_when_auth_off(self) -> None:
        self.service.settings.authentication_web = False
        self.assertEqual(self.client.get("/web/login", follow_redirects=False).headers["location"], "/web/")


class Users(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.service.auth.create_first_user("admin", "pw")
        self.enable_auth()
        self.client.post("/web/login", data={"username": "admin", "password": "pw"})
        self.me = next(iter(self.service.auth.users))

    def test_add_edit_delete(self) -> None:
        response = self.client.post("/web/users", data={"username": "bob", "password": "pw", "confirm": "pw",
                                                         LEVEL_WEB: "on"}, headers=HX)  # fmt: skip
        self.assertTrue(triggers(response)["close-dialog"])
        bob = next(uid for uid in self.service.auth.users if uid != self.me)
        self.assertIn("bob", self.client.get("/web/users/list").text)
        self.client.post(f"/web/users/{bob}", data={"username": "bob", LEVEL_API: "on"}, headers=HX)
        data = self.service.auth.read_user_data(bob)
        self.assertEqual((data[LEVEL_API], data[LEVEL_WEB]), (True, False))
        self.service.auth.authenticate("bob", "pw")  # password unchanged when left empty
        self.client.post(f"/web/users/{bob}/delete", headers=HX)
        self.assertNotIn(bob, self.service.auth.users)

    def test_refusals(self) -> None:
        response = self.client.post("/web/users", data={"username": "x", "password": "a", "confirm": "b"}, headers=HX)
        self.assertIn("The passwords do not match", response.text)
        response = self.client.post(
            "/web/users", data={"username": "admin", "password": "a", "confirm": "a"}, headers=HX
        )
        self.assertIn("User already exists", response.text)
        response = self.client.post(f"/web/users/{self.me}/delete", headers=HX)
        self.assertEqual(triggers(response)["toast"], "You cannot delete your own account")
        response = self.client.post(f"/web/users/{self.me}", data={"username": "admin"}, headers=HX)
        self.assertIn("You cannot remove your own web access", response.text)
        self.assertIn(self.me, self.service.auth.users)
