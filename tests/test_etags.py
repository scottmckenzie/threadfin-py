from webfixture import AppTestCase

PATHS = ("/m3u/threadfin.m3u", "/xmltv/threadfin.xml", "/xmltv/threadfin.xml.gz")


class ETagTests(AppTestCase):
    def test_conditional_get(self) -> None:
        for path in PATHS:
            with self.subTest(path=path):
                first = self.client.get(path)
                self.assertEqual(first.status_code, 200)
                etag = first.headers["etag"]
                self.assertTrue(etag.startswith('"') and etag.endswith('"'), etag)

                for header in (etag, f"W/{etag}", f'"other", {etag}', f'"other" ,  W/{etag} ', "*", " * "):
                    with self.subTest(if_none_match=header):
                        again = self.client.get(path, headers={"If-None-Match": header})
                        self.assertEqual(again.status_code, 304)
                        self.assertEqual(again.content, b"")
                        self.assertEqual(again.headers["etag"], etag)

                for header in ('"nope"', etag[:-2] + '"', ""):
                    with self.subTest(if_none_match=header):
                        full = self.client.get(path, headers={"If-None-Match": header})
                        self.assertEqual(full.status_code, 200)
                        self.assertEqual(full.content, first.content)

    def test_head_gets_304_too(self) -> None:
        for path in PATHS:
            with self.subTest(path=path):
                etag = self.client.head(path).headers["etag"]
                self.assertEqual(self.client.head(path, headers={"If-None-Match": etag}).status_code, 304)

    def test_m3u_etag_follows_the_text(self) -> None:
        plain = self.client.get("/m3u/threadfin.m3u").headers["etag"]
        news = self.client.get("/m3u/threadfin.m3u?group-title=News").headers["etag"]
        self.assertNotEqual(plain, news)
        self.assertEqual(self.client.get("/m3u/threadfin.m3u").headers["etag"], plain)

    def test_guide_etags_differ(self) -> None:
        xml = self.client.get("/xmltv/threadfin.xml").headers["etag"]
        gz = self.client.get("/xmltv/threadfin.xml.gz").headers["etag"]
        self.assertNotEqual(xml, gz)

    def test_refused_and_missing_have_no_etag(self) -> None:
        self.service.settings.authentication_m3u = True
        refused = self.client.get("/m3u/threadfin.m3u", headers={"If-None-Match": "*"})
        self.assertEqual(refused.status_code, 403)
        self.assertNotIn("etag", refused.headers)
        self.service.xmltv_path.unlink()
        missing = self.client.get("/xmltv/threadfin.xml", headers={"If-None-Match": "*"})
        self.assertEqual(missing.status_code, 404)
