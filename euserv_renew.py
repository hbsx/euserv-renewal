"""Renew an EUserv contract and complete an email verification code when requested.

Credentials are supplied only by environment variables (usually through a local .env
file).  The final renewal confirmation is intentionally gated behind --commit.
"""
import argparse
import http.client
import imaplib
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


def load_dotenv(path=".env"):
    """Tiny .env reader so secrets do not need another Python package."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError("Missing required setting: {}".format(name))
    return value


def decoded(value):
    out = []
    for piece, charset in decode_header(value or ""):
        if isinstance(piece, bytes):
            out.append(piece.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(piece)
    return "".join(out)


def message_text(msg):
    parts = msg.walk() if msg.is_multipart() else [msg]
    chunks = []
    for part in parts:
        if part.get_content_type() != "text/plain":
            continue
        payload = part.get_payload(decode=True)
        if payload:
            chunks.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
    return "\n".join(chunks)


def open_gmail_imap():
    """Open Gmail IMAP directly or through the optional Gmail-only SOCKS5 proxy."""
    imap_host = os.getenv("GMAIL_IMAP_HOST", "imap.gmail.com").strip()
    timeout = int(os.getenv("GMAIL_IMAP_TIMEOUT", "20"))
    proxy_host = os.getenv("GMAIL_SOCKS_HOST", "").strip()
    proxy_port_value = os.getenv("GMAIL_SOCKS_PORT", "").strip()
    if not proxy_host:
        return imaplib.IMAP4_SSL(imap_host, 993, timeout=timeout)

    if not proxy_port_value:
        raise RuntimeError("GMAIL_SOCKS_PORT is required when GMAIL_SOCKS_HOST is set")
    try:
        import socks
    except ImportError as error:
        raise RuntimeError("Gmail SOCKS proxy requires the PySocks package (Debian: python3-socks)") from error

    proxy_port = int(proxy_port_value)

    class SocksIMAP4SSL(imaplib.IMAP4_SSL):
        def _create_socket(self, socket_timeout):
            raw_socket = socks.create_connection(
                (self.host, self.port),
                timeout=socket_timeout,
                proxy_type=socks.SOCKS5,
                proxy_addr=proxy_host,
                proxy_port=proxy_port,
                proxy_rdns=True,
            )
            return self.ssl_context.wrap_socket(raw_socket, server_hostname=self.host)

    return SocksIMAP4SSL(imap_host, 993, timeout=timeout)


def wait_for_gmail_code(not_before, timeout):
    """Poll Gmail INBOX for a recent EUserv verification code."""
    address = required("GMAIL_ADDRESS")
    password = required("GMAIL_APP_PASSWORD").replace(" ", "")
    sender_hint = os.getenv("GMAIL_FROM_HINT", "euserv").lower()
    subject_hint = os.getenv("GMAIL_SUBJECT_HINT", "").lower()
    pattern = re.compile(os.getenv("CODE_REGEX", r"(?<!\d)(\d{6})(?!\d)"))
    deadline = time.time() + timeout

    while time.time() < deadline:
        client = open_gmail_imap()
        try:
            client.login(address, password)
            client.select("INBOX")
            status, data = client.search(None, "ALL")
            if status != "OK":
                raise RuntimeError("Could not search Gmail INBOX")
            for uid in reversed(data[0].split()[-20:]):
                status, raw = client.fetch(uid, "(RFC822)")
                if status != "OK" or not raw or not raw[0]:
                    continue
                msg = message_from_bytes(raw[0][1])
                date_value = msg.get("Date")
                try:
                    from email.utils import parsedate_to_datetime
                    sent_at = parsedate_to_datetime(date_value).astimezone(timezone.utc).timestamp()
                except Exception:
                    sent_at = time.time()
                if sent_at < not_before - 120:
                    continue
                sender = decoded(msg.get("From", "")).lower()
                subject = decoded(msg.get("Subject", "")).lower()
                body = message_text(msg)
                if sender_hint and sender_hint not in sender:
                    continue
                if subject_hint and subject_hint not in subject:
                    continue
                match = pattern.search(subject + "\n" + body)
                if match:
                    return match.group(1)
        finally:
            try:
                client.logout()
            except Exception:
                pass
        time.sleep(8)
    raise TimeoutException("No matching verification email arrived before timeout")


def notify_telegram(message):
    """Send an optional Telegram notification without exposing credentials in logs."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram notification skipped: bot token or chat ID is not configured.")
        return
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode("utf-8")
    path = "/bot{}/sendMessage".format(token)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    proxy_host = os.getenv("TELEGRAM_SOCKS_HOST", "").strip()
    proxy_port_value = os.getenv("TELEGRAM_SOCKS_PORT", "").strip()
    try:
        if proxy_host:
            if not proxy_port_value:
                raise RuntimeError("TELEGRAM_SOCKS_PORT is required when TELEGRAM_SOCKS_HOST is set")
            try:
                import socks
            except ImportError as error:
                raise RuntimeError("Telegram SOCKS proxy requires the PySocks package (Debian: python3-socks)") from error

            class SocksHTTPSConnection(http.client.HTTPSConnection):
                def connect(self):
                    raw_socket = socks.create_connection(
                        (self.host, self.port),
                        timeout=self.timeout,
                        proxy_type=socks.SOCKS5,
                        proxy_addr=proxy_host,
                        proxy_port=int(proxy_port_value),
                        proxy_rdns=True,
                    )
                    self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)

            connection = SocksHTTPSConnection(
                "api.telegram.org", timeout=15, context=ssl.create_default_context()
            )
            try:
                connection.request("POST", path, body=payload, headers=headers)
                response = connection.getresponse()
                response.read()
                if response.status != 200:
                    raise RuntimeError("Telegram returned HTTP {}".format(response.status))
            finally:
                connection.close()
        else:
            request = urllib.request.Request(
                "https://api.telegram.org{}".format(path),
                data=payload,
                method="POST",
                headers=headers,
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                if response.status != 200:
                    raise RuntimeError("Telegram returned HTTP {}".format(response.status))
        print("Telegram renewal notification sent.")
    except Exception as error:
        print("Telegram notification failed: {}".format(error))


def first_visible(driver, selectors, timeout=0):
    deadline = time.time() + timeout
    while True:
        for by, value in selectors:
            for element in driver.find_elements(by, value):
                if element.is_displayed() and element.is_enabled():
                    return element
        if time.time() >= deadline:
            return None
        time.sleep(0.5)


def click_by_text(driver, labels, timeout):
    conditions = []
    for label in labels:
        normalized = label.lower()
        conditions.append("contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{}')".format(normalized))
        conditions.append("self::input and contains(translate(@value, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{}')".format(normalized))
    xpath = "//*[self::a or self::button or self::input][{}]".format(
        " or ".join(conditions)
    )
    element = WebDriverWait(driver, timeout).until(lambda d: first_visible(d, [(By.XPATH, xpath)]))
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    element.click()
    return element


def wait_for_manual_captcha(driver, timeout):
    """Pause for a human to solve any visual CAPTCHA shown by the provider.

    This deliberately does not attempt to bypass EUserv's anti-bot check.
    """
    captcha = first_visible(driver, [
        (By.CSS_SELECTOR, "input[name*='captcha']"),
        (By.CSS_SELECTOR, "input[id*='captcha']"),
    ], 3)
    if not captcha:
        return
    print("A visual CAPTCHA is displayed. Complete it in the browser, then press Enter here.")
    input()


def fill(driver, selectors, value, timeout):
    element = WebDriverWait(driver, timeout).until(lambda d: first_visible(d, selectors))
    element.clear()
    element.send_keys(value)
    return element


def main():
    parser = argparse.ArgumentParser(description="EUserv renewal helper")
    parser.add_argument("--commit", action="store_true", help="allow the final renewal submission")
    parser.add_argument("--timeout", type=int, default=int(os.getenv("TIMEOUT_SECONDS", "25")))
    args = parser.parse_args()
    load_dotenv()

    username, password = required("EUSERV_USERNAME"), required("EUSERV_PASSWORD")
    options = webdriver.ChromeOptions()
    if os.getenv("HEADLESS", "false").lower() == "true":
        options.add_argument("--headless=new")
    chrome_binary = os.getenv("CHROME_BINARY", "").strip()
    if chrome_binary:
        options.binary_location = chrome_binary
    if sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0:
        # Chromium otherwise refuses to start as root in a Debian container.
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
    profile_setting = os.getenv("CHROME_PROFILE_DIR", "browser-profile")
    profile_dir = Path(profile_setting)
    if not profile_dir.is_absolute():
        profile_dir = Path.cwd() / profile_dir
    profile_dir.mkdir(parents=True, exist_ok=True)
    options.add_argument("--user-data-dir={}".format(profile_dir))
    options.add_argument("--window-size=1400,1000")
    driver = webdriver.Chrome(options=options)
    started = datetime.now(timezone.utc).timestamp()
    try:
        driver.get(os.getenv("EUSERV_LOGIN_URL", "https://support.euserv.de/"))
        login_selectors = [(By.NAME, "email"), (By.CSS_SELECTOR, "input[type='email']"), (By.NAME, "username"), (By.NAME, "login"), (By.ID, "username")]
        login_box = first_visible(driver, login_selectors, 5)
        if login_box:
            login_box.clear()
            login_box.send_keys(username)
            fill(driver, [(By.CSS_SELECTOR, "input[type='password']"), (By.NAME, "password"), (By.ID, "password")], password, args.timeout)
            wait_for_manual_captcha(driver, args.timeout)
            click_by_text(driver, ["login", "anmelden", "sign in"], args.timeout)
        else:
            print("Reusing existing EUserv browser session; no login submitted.")

        # Some accounts ask for an email code immediately after login.
        code_box = first_visible(driver, [(By.CSS_SELECTOR, "input[name*='code']"), (By.CSS_SELECTOR, "input[name*='otp']"), (By.CSS_SELECTOR, "input[autocomplete='one-time-code']")], 8)
        if code_box:
            print("Waiting for EUserv login verification email…")
            code_box.send_keys(wait_for_gmail_code(started, 180))
            click_by_text(driver, ["confirm", "verify", "bestätigen", "weiter", "continue"], args.timeout)

        contract = os.getenv("EUSERV_CONTRACT_NUMBER", "").strip()
        if contract:
            click_by_text(driver, [contract], args.timeout)
        print("Looking for renewal action…")
        try:
            click_by_text(driver, ["verlängern", "verlaengern", "renew", "extension"], args.timeout)
        except TimeoutException:
            diagnostic = Path.cwd() / "no-renewal-action.png"
            try:
                driver.save_screenshot(str(diagnostic))
                print("Diagnostic screenshot saved to {}.".format(diagnostic))
            except Exception:
                pass
            print("No renewal action is currently available. The contract may already be renewed or not yet be inside its renewal window.")
            if args.commit:
                notify_telegram(
                    "⚠️ EUserv automatic renewal found no renewal action. "
                    "The contract may already be renewed or outside its renewal window; please check the account."
                )
            return

        # If the renewal flow sends a second email code, fill it in.
        code_box = first_visible(driver, [(By.CSS_SELECTOR, "input[name*='code']"), (By.CSS_SELECTOR, "input[name*='otp']"), (By.CSS_SELECTOR, "input[autocomplete='one-time-code']")], 8)
        if code_box:
            print("Waiting for EUserv renewal verification email…")
            code_box.send_keys(wait_for_gmail_code(started, 180))

        if not args.commit:
            print("Reached the renewal confirmation step. No final action was sent. Re-run with --commit to submit.")
            return
        click_by_text(driver, ["confirm renewal", "renew now", "verlängern", "bestätigen", "confirm"], args.timeout)
        success_markers = (
            "contract extension was successful",
            "contract successfully extended",
            "contract extension successful",
            "vertragsverlängerung erfolgreich",
        )
        try:
            WebDriverWait(driver, args.timeout).until(
                lambda d: any(marker in d.page_source.lower() for marker in success_markers)
            )
            print("Renewal confirmed by EUserv.")
            notify_telegram("✅ EUserv contract renewal succeeded.")
        except TimeoutException:
            print("Renewal was submitted, but EUserv did not show a recognized success message. No Telegram success notice was sent.")
            notify_telegram(
                "❌ EUserv renewal was submitted, but the website did not show a recognized success message. "
                "Please check the account and systemd log."
            )
    finally:
        if os.getenv("HEADLESS", "false").lower() == "true":
            driver.quit()
        else:
            print("Browser left open for review. Close it when finished.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("ERROR [{}]: {}".format(type(error).__name__, error), file=sys.stderr)
        if "--commit" in sys.argv:
            try:
                load_dotenv()
                summary = str(error).replace("\r", " ").replace("\n", " ").strip()
                if len(summary) > 180:
                    summary = summary[:177] + "..."
                detail = ": {}".format(summary) if summary else ""
                notify_telegram(
                    "❌ EUserv automatic renewal failed [{}]{}. Please check the systemd log.".format(
                        type(error).__name__, detail
                    )
                )
            except Exception as notify_error:
                print("Failure notification could not be sent: {}".format(notify_error), file=sys.stderr)
        sys.exit(1)
