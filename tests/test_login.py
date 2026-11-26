"""只使用虚构账号和模拟页面；不登录真实网站，不调用真实送审。"""
from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import credential_store
import songshen


ACCOUNTS = {
    "casmart": {"username": "synthetic-mall-user", "password": "test-mall-only"},
    "smu": {"username": "synthetic-school-user", "password": "test-school-only"},
}
SCHOOL_URL = (
    "https://uis.smu.edu.cn/login.jsp"
    "?redirect=https%3a%2f%2fmember.casmart.com.cn%2fapi%2fv2%2fsso_smu%2flogin"
    "&tmmuUserMemberId=synthetic"
)


class Field:
    def __init__(self, input_type="text", text="", displayed=True, enabled=True, **attributes):
        self.input_type = input_type
        self.text = text
        self.displayed = displayed
        self.enabled = enabled
        self.attributes = attributes

    def get_attribute(self, name):
        return self.input_type if name == "type" else ""

    def get_dom_attribute(self, name):
        return self.attributes.get(name)

    def is_displayed(self):
        return self.displayed

    def is_enabled(self):
        return self.enabled


class PageDriver:
    """模拟窗口和可见登录/退出标记，不模拟服务器的认证成功。"""
    def __init__(self, url="https://member.casmart.com.cn/index", authenticated=True):
        self.pages = {"main": (url, authenticated, [])}
        self.active = "main"
        self.switch_to = Mock()
        self.switch_to.window.side_effect = self._switch
        self.visited = []
        self.redirect_to_school = False

    def _switch(self, handle):
        self.active = handle

    @property
    def window_handles(self):
        return list(self.pages)

    @property
    def current_url(self):
        return self.pages[self.active][0]

    def find_elements(self, by, value):
        _, authenticated, inputs = self.pages[self.active]
        if value == "input":
            return inputs
        if "退出" in value:
            return [Field(text="[退出]")] if authenticated else []
        return []

    def execute_script(self, script):
        if script == "return document.readyState":
            return "complete"
        raise AssertionError(f"意外脚本: {script}")

    def get(self, url):
        self.visited.append(url)
        if self.redirect_to_school:
            self.pages[self.active] = ("https://uis.smu.edu.cn/login.jsp", False, [])
        else:
            self.pages[self.active] = (url, True, [])


@unittest.skipUnless(os.name == "nt", "DPAPI 需要 Windows")
class CredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "credentials.bin"

    def test_encrypted_roundtrip_and_delete(self):
        self.assertEqual(credential_store.load_credentials(self.path), {})
        credential_store.save_credentials(ACCOUNTS, self.path)
        encrypted = self.path.read_bytes()
        for account in ACCOUNTS.values():
            for value in account.values():
                self.assertNotIn(value.encode(), encrypted)
        self.assertEqual(credential_store.load_credentials(self.path), ACCOUNTS)
        credential_store.delete_credentials(self.path)
        credential_store.delete_credentials(self.path)
        self.assertFalse(self.path.exists())

    def test_corrupted_file_is_not_used(self):
        self.path.write_bytes(b"not-a-supported-credential-file")
        with self.assertRaises(ValueError):
            credential_store.load_credentials(self.path)
        credential_store.save_credentials(ACCOUNTS, self.path)
        encrypted = bytearray(self.path.read_bytes())
        encrypted[-1] ^= 1
        self.path.write_bytes(encrypted)
        with self.assertRaises((OSError, ValueError)):
            credential_store.load_credentials(self.path)

    def test_failed_replacement_preserves_previous_credentials(self):
        credential_store.save_credentials(ACCOUNTS, self.path)
        original = self.path.read_bytes()
        with patch.object(credential_store.os, "replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                credential_store.save_credentials({"smu": ACCOUNTS["smu"]}, self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_invalid_accounts_are_not_written(self):
        for accounts in ({"other": ACCOUNTS["smu"]}, {"smu": {"username": "x"}}, {"smu": {"username": "x", "password": ""}}):
            with self.subTest(accounts=accounts):
                with self.assertRaises(ValueError):
                    credential_store.save_credentials(accounts, self.path)
        self.assertFalse(self.path.exists())


class LoginTests(unittest.TestCase):
    def test_credentials_are_bound_to_exact_https_origins(self):
        self.assertEqual(songshen.login_service("https://uis.smu.edu.cn/login.jsp"), "smu")
        self.assertEqual(songshen.login_service("https://member.casmart.com.cn/login"), "casmart")
        for url in (
            "http://uis.smu.edu.cn/login.jsp", "https://uis.smu.edu.cn.evil.test/login",
            "https://evil.test/?next=https://uis.smu.edu.cn", "https://uis.smu.edu.cn@evil.test",
            "https://member.casmart.com.cn.evil.test", "https://uis.smu.edu.cn:444/login",
            "https://user@uis.smu.edu.cn/login", "https://uis.smu.edu.cn:bad/login", "about:blank",
            "https://uis.smu.edu.cn/account/change-password",
        ):
            with self.subTest(url=url):
                self.assertIsNone(songshen.login_service(url))

    def test_login_fields_do_not_overwrite_captcha(self):
        username, password, captcha = Field(), Field("password"), Field()
        driver = PageDriver(authenticated=False)
        driver.pages["main"] = (driver.current_url, False, [username, password, captcha])
        self.assertEqual(songshen.find_login_fields(driver), (username, password))
        driver.pages["main"] = (driver.current_url, False, [username, password, Field("password")])
        self.assertEqual(songshen.find_login_fields(driver), (None, None))

    def test_school_fields_ignore_other_passwords_captcha_and_readonly_copy(self):
        username = Field(placeholder="校园网账号")
        password = Field("password", placeholder="密码")
        captcha = Field(placeholder="验证码")
        driver = PageDriver(authenticated=False)
        driver.pages["main"] = (
            driver.current_url, False,
            [Field("password"), Field(placeholder="校园网账号", readonly="true"),
             username, password, captcha, Field("password")],
        )
        self.assertEqual(songshen.find_school_login_fields(driver), (username, password))
        driver.pages["main"] = (driver.current_url, False, [username, captcha, Field("password")])
        self.assertEqual(songshen.find_school_login_fields(driver), (None, None))

    def test_school_autofill_runs_on_first_poll_with_editable_fields(self):
        for state in ("ready", "disabled", "replaced"):
            with self.subTest(state=state):
                driver = PageDriver(SCHOOL_URL, authenticated=False)
                username = Field(placeholder="校园网账号")
                password = Field("password", enabled=state != "disabled")
                captcha = Field(placeholder="验证码")
                driver.pages["main"] = (SCHOOL_URL, False, [username, password, captcha])
                if state == "replaced":
                    old_element = Mock()
                    old_element.is_displayed.side_effect = songshen.StaleElementReferenceException
                    driver.pages["main"] = (SCHOOL_URL, False, [old_element])
                waited = []

                def browser_loads_form(seconds):
                    waited.append(seconds)
                    if state != "ready" and len(waited) == 1:
                        password.enabled = True
                        driver.pages["main"] = (SCHOOL_URL, False, [username, password, captcha])

                def fill(first, second, credentials):
                    self.assertEqual((first, second), (username, password))
                    self.assertEqual(credentials, ACCOUNTS["smu"])
                    self.assertEqual(len(waited), 0 if state == "ready" else 1)
                    driver.pages["main"] = ("https://member.casmart.com.cn/index", True, [])
                    return True

                with patch.object(songshen.time, "sleep", side_effect=browser_loads_form), \
                     patch.object(songshen, "fill_school_account", side_effect=fill) as school_fill, \
                     patch.object(songshen, "submit_login") as submit, redirect_stdout(StringIO()):
                    self.assertTrue(songshen.auto_login_if_needed(driver, ACCOUNTS, timeout=1))
                school_fill.assert_called_once()
                submit.assert_not_called()

    def test_local_sso_entry_keeps_query_and_rejects_unsafe_callbacks(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "login-url.txt"
            path.write_text(SCHOOL_URL + "\n", encoding="utf-8-sig")
            self.assertEqual(songshen.load_school_login_url(path), SCHOOL_URL)
            for url in (
                SCHOOL_URL + "&redirect=https://evil.test",
                SCHOOL_URL.replace("member.casmart.com.cn", "evil.test"),
                SCHOOL_URL.replace("uis.smu.edu.cn", "evil.test"),
                SCHOOL_URL.replace("login.jsp", "change-password"),
                SCHOOL_URL + "#fragment",
            ):
                with self.subTest(url=url):
                    path.write_text(url, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        songshen.load_school_login_url(path)

    def test_login_action_ignores_tabs_and_disabled_buttons(self):
        tab = Field(text="账号登录")
        disabled = Field(text="登录", enabled=False)
        submit = Field(text="登 录")
        with patch.object(songshen, "find_visible_elements", return_value=[tab, disabled, submit]):
            self.assertIs(songshen.find_login_button(Mock()), submit)

    def test_login_requires_positive_authenticated_marker(self):
        self.assertTrue(songshen.is_authenticated_casmart(PageDriver()))
        self.assertFalse(songshen.is_authenticated_casmart(PageDriver(authenticated=False)))
        self.assertFalse(songshen.is_authenticated_casmart(PageDriver("https://uis.smu.edu.cn/login.jsp")))
        driver = PageDriver()
        driver.pages["main"] = (driver.current_url, True, [Field("password")])
        self.assertFalse(songshen.is_authenticated_casmart(driver))

    def test_completed_parent_window_is_found_after_popup_login(self):
        driver = PageDriver()
        driver.pages["school"] = ("https://uis.smu.edu.cn/login.jsp", False, [Field("password")])
        driver.active = "school"
        self.assertEqual(songshen.select_login_window(driver), ("main", "ready"))
        self.assertEqual(driver.active, "main")

    def test_two_stage_login_fills_separate_accounts_once_and_waits_for_captcha(self):
        driver = Mock(window_handles=["main", "school"])
        mall_user, mall_password = Field(), Field("password")
        school_user, school_password = Field(), Field("password")
        sso_button, login_button = Mock(), Mock()
        state = {}
        events = iter(("mall", "sso", "school", "school", "ready"))

        def select(_driver):
            state["event"] = next(events)
            event = state["event"]
            return ("school", "smu") if event == "school" else ("main", "ready" if event == "ready" else "casmart")

        def fields(_driver):
            return mall_user, mall_password

        def inputs(_driver):
            return [mall_user, mall_password, Field("checkbox")]

        def action(_driver, _labels):
            return sso_button if state["event"] == "sso" else None

        with patch.object(songshen, "select_login_window", side_effect=select), \
             patch.object(songshen, "find_login_fields", side_effect=fields), \
             patch.object(songshen, "find_school_login_fields", return_value=(school_user, school_password)), \
             patch.object(songshen, "visible_inputs", side_effect=inputs), \
             patch.object(songshen, "find_login_action", side_effect=action), \
             patch.object(songshen, "find_login_button", return_value=login_button), \
             patch.object(songshen, "set_input_value") as fill, \
             patch.object(songshen, "fill_school_account", return_value=True) as school_fill, \
             patch.object(songshen, "submit_login") as submit, \
             patch.object(songshen, "safe_click", return_value=True) as click, \
             patch.object(songshen.time, "sleep"), redirect_stdout(StringIO()):
            self.assertTrue(songshen.auto_login_if_needed(driver, ACCOUNTS, timeout=10))
        self.assertEqual(fill.call_count, 2)
        fill.assert_any_call(driver, mall_user, ACCOUNTS["casmart"]["username"])
        fill.assert_any_call(driver, mall_password, ACCOUNTS["casmart"]["password"])
        school_fill.assert_called_once_with(school_user, school_password, ACCOUNTS["smu"])
        submit.assert_called_once_with(driver, login_button, mall_password)
        click.assert_called_once_with(driver, sso_button)

    def test_unrecognized_origin_times_out_without_filling(self):
        driver = PageDriver("https://unknown.example.test/login", authenticated=False)
        with patch.object(songshen.time, "monotonic", side_effect=[0, 0, 1]), \
             patch.object(songshen.time, "sleep"), \
             patch.object(songshen, "set_input_value") as fill, redirect_stdout(StringIO()):
            self.assertFalse(songshen.auto_login_if_needed(driver, ACCOUNTS, timeout=1))
        fill.assert_not_called()

    def test_navigation_uses_current_session_and_discards_redirect(self):
        driver = PageDriver("https://member.casmart.com.cn/index?client-key=synthetic-new-key&sys_channel=false&redirect=https%3A%2F%2Fevil.test")
        with patch.object(driver, "execute_script", side_effect=["loading", "complete"]) as ready, \
             patch.object(songshen.time, "sleep"), patch.object(songshen, "wait_loading_finished"), redirect_stdout(StringIO()):
            self.assertTrue(songshen.ensure_target_page(driver))
        self.assertEqual(ready.call_count, 2)
        target = urlsplit(driver.visited[0])
        self.assertEqual(target.hostname, "member.casmart.com.cn")
        self.assertEqual(target.path, songshen.TARGET_PATH)
        self.assertEqual(parse_qs(target.query), {"client-key": ["synthetic-new-key"], "sys_channel": ["false"]})

    def test_school_page_is_not_forcibly_navigated_away(self):
        driver = PageDriver("https://uis.smu.edu.cn/login.jsp", authenticated=False)
        with redirect_stdout(StringIO()):
            self.assertFalse(songshen.ensure_target_page(driver))
        self.assertEqual(driver.visited, [])

    def test_session_expiry_at_list_navigation_stops(self):
        driver = PageDriver()
        driver.redirect_to_school = True
        with patch.object(songshen, "WAIT_TIME", 0), redirect_stdout(StringIO()):
            self.assertFalse(songshen.ensure_target_page(driver))

    def test_main_does_not_submit_before_authentication_or_in_login_only_mode(self):
        for login_ok, target_ok, login_only in ((False, True, False), (True, False, False), (True, True, True)):
            with self.subTest(login_ok=login_ok, target_ok=target_ok, login_only=login_only), \
                 patch.object(songshen, "load_school_login_url", return_value=SCHOOL_URL), \
                 patch.object(songshen, "ask_credentials", return_value={}), \
                 patch.object(songshen, "init_driver", return_value=Mock()), \
                 patch.object(songshen, "auto_login_if_needed", return_value=login_ok), \
                 patch.object(songshen, "ensure_target_page", return_value=target_ok), \
                 patch.object(songshen, "process_current_page") as process, redirect_stdout(StringIO()):
                songshen.main(["--login-only"] if login_only else [])
                process.assert_not_called()

    def test_main_opens_school_first_and_keeps_mall_as_fallback(self):
        for args, start_url, direct in (([], SCHOOL_URL, True), (["--via-casmart"], songshen.TARGET_URL, False)):
            driver = Mock()
            with self.subTest(direct=direct), \
                 patch.object(songshen, "load_school_login_url", return_value=SCHOOL_URL), \
                 patch.object(songshen, "ask_credentials", return_value=ACCOUNTS) as ask, \
                 patch.object(songshen, "init_driver", return_value=driver), \
                 patch.object(songshen, "auto_login_if_needed", return_value=False), redirect_stdout(StringIO()):
                songshen.main(["--login-only", *args])
            driver.get.assert_called_once_with(start_url)
            driver.execute_script.assert_not_called()
            ask.assert_called_once_with(login_only=True, direct=direct)


if __name__ == "__main__":
    unittest.main()
