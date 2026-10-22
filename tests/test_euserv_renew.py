import unittest
from email.message import EmailMessage
from unittest.mock import patch

import euserv_renew as app


class FakeImap:
    def __init__(self, messages=None, select_error=None):
        self.messages = list(messages or [])
        self.select_error = select_error
        self.search_count = 0
        self.logout_count = 0

    def select(self, _mailbox, readonly=False):
        if self.select_error:
            error = self.select_error
            self.select_error = None
            raise error
        return "OK", [b""]

    def search(self, _charset, _criteria):
        self.search_count += 1
        visible = min(self.search_count, len(self.messages))
        return "OK", [b" ".join(str(index + 1).encode() for index in range(visible))]

    def fetch(self, uid, _query):
        message = self.messages[int(uid) - 1]
        metadata = b'1 (INTERNALDATE "22-Oct-2026 07:30:00 +0800" BODY[] {1}'
        return "OK", [(metadata, message.as_bytes())]

    def logout(self):
        self.logout_count += 1


def pin_message(code="570732", html_only=False):
    message = EmailMessage()
    message["From"] = "EUserv Support <no-reply@euserv.com>"
    message["Subject"] = "EUserv - PIN for the Confirmation of a Security Check"
    message["Date"] = "Thu, 22 Oct 2026 07:30:00 +0800"
    if html_only:
        message.set_content("<html><body><p>PIN:</p><strong>{}</strong></body></html>".format(code), subtype="html")
    else:
        message.set_content("PIN:\n{}\n".format(code))
    return message


class MailParsingTests(unittest.TestCase):
    def test_plain_text_is_preferred(self):
        message = EmailMessage()
        message.set_content("PIN: 111111")
        message.add_alternative("<b>PIN: 222222</b>", subtype="html")
        self.assertIn("111111", app.message_text(message))
        self.assertNotIn("222222", app.message_text(message))

    def test_html_only_message_is_readable(self):
        body = app.message_text(pin_message(html_only=True))
        self.assertIn("PIN:", body)
        self.assertIn("570732", body)

    def test_contract_number_boundary(self):
        self.assertTrue(app.contract_number_in_text("Contract 444166", "444166"))
        self.assertFalse(app.contract_number_in_text("Contract 4441667", "444166"))
        self.assertEqual("444166", app.canonical_contract_number(" 000-444166 "))

    def test_action_label_ignores_trailing_decoration_only(self):
        self.assertEqual("renew now", app.normalized_label("  Renew   now » "))
        self.assertEqual("extend contract", app.normalized_label("Extend contract›"))
        self.assertEqual("request extension", app.normalized_label("Request extension →"))
        self.assertNotEqual("renew", app.normalized_label("Renew contract"))


class GmailPollingTests(unittest.TestCase):
    @patch.dict(
        "os.environ",
        {
            "GMAIL_ADDRESS": "test@example.com",
            "GMAIL_APP_PASSWORD": "example-app-password",
            "GMAIL_FROM_HINT": "euserv.com",
            "GMAIL_SUBJECT_HINT": "PIN for the Confirmation",
        },
        clear=False,
    )
    @patch("euserv_renew.gmail_poll_delay", return_value=None)
    def test_connection_is_reused_while_polling(self, _delay):
        old_candidate = pin_message("111111")
        old_candidate.replace_header("Subject", "Unrelated message")
        client = FakeImap([old_candidate, pin_message()])
        with patch("euserv_renew.open_authenticated_gmail", return_value=client) as connect:
            code = app.wait_for_gmail_code(0, 2)
        self.assertEqual("570732", code)
        connect.assert_called_once()
        self.assertEqual(2, client.search_count)
        self.assertEqual(1, client.logout_count)

    @patch.dict(
        "os.environ",
        {
            "GMAIL_ADDRESS": "test@example.com",
            "GMAIL_APP_PASSWORD": "example-app-password",
            "GMAIL_FROM_HINT": "euserv.com",
            "GMAIL_SUBJECT_HINT": "PIN for the Confirmation",
        },
        clear=False,
    )
    @patch("euserv_renew.gmail_poll_delay", return_value=None)
    def test_transient_error_reconnects(self, _delay):
        broken = FakeImap(select_error=OSError("temporary network failure"))
        working = FakeImap([pin_message()])
        with patch(
            "euserv_renew.open_authenticated_gmail",
            side_effect=[broken, working],
        ) as connect:
            code = app.wait_for_gmail_code(0, 2)
        self.assertEqual("570732", code)
        self.assertEqual(2, connect.call_count)
        self.assertEqual(1, broken.logout_count)
        self.assertEqual(1, working.logout_count)

    @patch.dict(
        "os.environ",
        {
            "GMAIL_ADDRESS": "test@example.com",
            "GMAIL_APP_PASSWORD": "wrong-app-password",
        },
        clear=False,
    )
    def test_authentication_failure_is_not_retried(self):
        with patch(
            "euserv_renew.open_authenticated_gmail",
            side_effect=app.GmailAuthenticationError("authentication failed"),
        ) as connect:
            with self.assertRaises(app.GmailAuthenticationError):
                app.wait_for_gmail_code(0, 2)
        connect.assert_called_once()


if __name__ == "__main__":
    unittest.main()
