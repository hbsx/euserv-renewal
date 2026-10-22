"""Renew an EUserv contract and complete an email verification code when requested.

Credentials are supplied only by environment variables (usually through a local .env
file).  The final renewal confirmation is intentionally gated behind --commit.
"""
import argparse
import html
import http.client
import imaplib
import os
import re
import signal
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import timezone
from email import message_from_bytes
from email.header import decode_header
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


class RenewalFailure(RuntimeError):
    """A terminal renewal outcome that has already been explained to the user."""


class GmailAuthenticationError(RuntimeError):
    """A non-transient Gmail authentication failure."""


def load_dotenv(path=".env"):
    """Tiny .env reader so secrets do not need another Python package."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
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
    """Return readable message text, preferring plain text over HTML."""
    parts = msg.walk() if msg.is_multipart() else [msg]
    plain_chunks = []
    html_chunks = []
    for part in parts:
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        if part.get_content_disposition() == "attachment":
            continue
        payload = part.get_payload(decode=True)
        if payload:
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            if content_type == "text/plain":
                plain_chunks.append(text)
            else:
                html_chunks.append(text)
    if plain_chunks:
        return "\n".join(plain_chunks)
    if not html_chunks:
        return ""
    rendered = "\n".join(html_chunks)
    rendered = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", rendered)
    rendered = re.sub(r"(?s)<[^>]+>", " ", rendered)
    return re.sub(r"\s+", " ", html.unescape(rendered)).strip()


def canonical_contract_number(value):
    """Normalize a contract number for comparisons without changing its meaning."""
    digits = re.sub(r"\D", "", value or "")
    return digits.lstrip("0") or ("0" if digits else "")


def message_timestamp(msg, fetch_metadata):
    """Return Gmail's arrival time, falling back to the message Date header."""
    try:
        internal_time = imaplib.Internaldate2tuple(fetch_metadata)
        if internal_time:
            return time.mktime(internal_time)
    except Exception:
        pass
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(msg.get("Date")).astimezone(timezone.utc).timestamp()
    except Exception:
        return None


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


def close_gmail_imap(client):
    if client is None:
        return
    try:
        client.logout()
    except Exception:
        pass


def open_authenticated_gmail(address, password):
    client = open_gmail_imap()
    try:
        client.login(address, password)
        return client
    except imaplib.IMAP4.error as error:
        close_gmail_imap(client)
        raise GmailAuthenticationError(
            "Gmail authentication failed; check the address, app password, and IMAP access"
        ) from error


def gmail_poll_delay(deadline):
    time.sleep(max(0, min(8, deadline - time.time())))


def wait_for_gmail_code(not_before, timeout, excluded_codes=None, subject_hint=None):
    """Poll Gmail for the newest recent PIN, ignoring old or already-used codes."""
    address = required("GMAIL_ADDRESS")
    password = required("GMAIL_APP_PASSWORD").replace(" ", "")
    sender_hint = os.getenv("GMAIL_FROM_HINT", "euserv").lower()
    if subject_hint is None:
        subject_hint = os.getenv("GMAIL_SUBJECT_HINT", "")
    subject_hint = subject_hint.lower()
    mailbox = os.getenv("GMAIL_MAILBOX", "INBOX").strip() or "INBOX"
    pattern = re.compile(os.getenv("CODE_REGEX", r"(?<!\d)(\d{6})(?!\d)"))
    excluded_codes = set(excluded_codes or ())
    deadline = time.time() + timeout
    client = None

    try:
        while time.time() < deadline:
            try:
                if client is None:
                    client = open_authenticated_gmail(address, password)
                status, _ = client.select(mailbox, readonly=True)
                if status != "OK":
                    raise imaplib.IMAP4.error("Could not select Gmail mailbox {!r}".format(mailbox))
                status, data = client.search(None, "ALL")
                if status != "OK":
                    raise imaplib.IMAP4.error("Could not search Gmail mailbox {!r}".format(mailbox))
                for uid in reversed(data[0].split()[-20:]):
                    # PEEK avoids changing the message's read/unread state where supported.
                    status, raw = client.fetch(uid, "(INTERNALDATE BODY.PEEK[])")
                    if status != "OK" or not raw or not raw[0] or not isinstance(raw[0], tuple):
                        continue
                    msg = message_from_bytes(raw[0][1])
                    sent_at = message_timestamp(msg, raw[0][0])
                    if sent_at is None or sent_at < not_before - 30:
                        continue
                    sender = decoded(msg.get("From", "")).lower()
                    subject = decoded(msg.get("Subject", "")).lower()
                    body = message_text(msg)
                    if sender_hint and sender_hint not in sender:
                        continue
                    if subject_hint and subject_hint not in subject:
                        continue
                    match = pattern.search(subject + "\n" + body)
                    if match and match.group(1) not in excluded_codes:
                        return match.group(1)
            except GmailAuthenticationError:
                raise
            except (imaplib.IMAP4.error, OSError, ssl.SSLError) as error:
                print("Temporary Gmail error; reconnecting: {}".format(error), file=sys.stderr)
                close_gmail_imap(client)
                client = None
            gmail_poll_delay(deadline)
    finally:
        close_gmail_imap(client)
    raise TimeoutException("No matching verification email arrived before timeout")


def wait_for_gmail_confirmation(not_before, timeout, contract_number=""):
    """Wait for the newest EUserv contract-extension confirmation email."""
    address = required("GMAIL_ADDRESS")
    password = required("GMAIL_APP_PASSWORD").replace(" ", "")
    sender_hint = os.getenv("GMAIL_FROM_HINT", "euserv").lower()
    mailbox = os.getenv("GMAIL_MAILBOX", "INBOX").strip() or "INBOX"
    expected_contract = canonical_contract_number(contract_number)
    deadline = time.time() + timeout
    client = None

    try:
        while time.time() < deadline:
            try:
                if client is None:
                    client = open_authenticated_gmail(address, password)
                status, _ = client.select(mailbox, readonly=True)
                if status != "OK":
                    raise imaplib.IMAP4.error("Could not select Gmail mailbox {!r}".format(mailbox))
                status, data = client.search(None, "ALL")
                if status != "OK":
                    raise imaplib.IMAP4.error("Could not search Gmail mailbox {!r}".format(mailbox))
                for uid in reversed(data[0].split()[-30:]):
                    status, raw = client.fetch(uid, "(INTERNALDATE BODY.PEEK[])")
                    if status != "OK" or not raw or not raw[0] or not isinstance(raw[0], tuple):
                        continue
                    msg = message_from_bytes(raw[0][1])
                    sent_at = message_timestamp(msg, raw[0][0])
                    if sent_at is None or sent_at < not_before - 30:
                        continue

                    sender = decoded(msg.get("From", "")).lower()
                    subject = decoded(msg.get("Subject", ""))
                    if sender_hint and sender_hint not in sender:
                        continue
                    subject_match = re.search(
                        r"extension\s+of\s+your\s+contract\s+([\d\s-]+)",
                        subject,
                        re.IGNORECASE,
                    )
                    if not subject_match:
                        continue
                    confirmed_contract = canonical_contract_number(subject_match.group(1))
                    if expected_contract and confirmed_contract != expected_contract:
                        continue

                    body = message_text(msg)
                    date_match = re.search(
                        r"has\s+been\s+extended\s+until\s+(\d{4}-\d{2}-\d{2})",
                        body,
                        re.IGNORECASE,
                    )
                    if date_match:
                        return confirmed_contract, date_match.group(1)
            except GmailAuthenticationError:
                raise
            except (imaplib.IMAP4.error, OSError, ssl.SSLError) as error:
                print("Temporary Gmail error; reconnecting: {}".format(error), file=sys.stderr)
                close_gmail_imap(client)
                client = None
            gmail_poll_delay(deadline)
    finally:
        close_gmail_imap(client)
    raise TimeoutException("No matching contract-extension confirmation email arrived before timeout")


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


def normalized_label(value):
    normalized = re.sub(r"\s+", " ", value or "").strip()
    normalized = re.sub(r"[»›→]+$", "", normalized).rstrip()
    return normalized.casefold()


def element_label(element):
    return normalized_label(element.get_attribute("value") or element.text or "")


def find_exact_clickable(search_root, labels, excluded_ids=None):
    expected = {normalized_label(label) for label in labels}
    excluded_ids = set(excluded_ids or ())
    selector = "a, button, input[type='submit'], input[type='button'], [role='button']"
    for element in search_root.find_elements(By.CSS_SELECTOR, selector):
        if element.id in excluded_ids:
            continue
        if element.is_displayed() and element.is_enabled() and element_label(element) in expected:
            return element
    return None


def click_exact_by_text(driver, labels, timeout, search_root=None, excluded_ids=None):
    def locate(_driver):
        root = search_root or _driver
        return find_exact_clickable(root, labels, excluded_ids)

    element = WebDriverWait(driver, timeout).until(locate)
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    element.click()
    return element


def wait_for_exact_clickable(driver, labels, timeout, search_root=None, excluded_ids=None):
    return WebDriverWait(driver, timeout).until(
        lambda current_driver: find_exact_clickable(
            search_root or current_driver,
            labels,
            excluded_ids,
        )
    )


def contract_number_in_text(text, contract_number):
    return bool(
        re.search(
            r"(?<!\d){}(?!\d)".format(re.escape(contract_number)),
            text or "",
        )
    )


def click_contract(driver, contract_number, timeout):
    def locate(_driver):
        candidates = []
        for element in _driver.find_elements(By.CSS_SELECTOR, "a, button"):
            if not element.is_displayed() or not element.is_enabled():
                continue
            label = element.get_attribute("textContent") or element.text or ""
            if contract_number_in_text(label, contract_number):
                candidates.append((len(normalized_label(label)), element))
        if not candidates:
            return None
        return sorted(candidates, key=lambda item: item[0])[0][1]

    element = WebDriverWait(driver, timeout).until(locate)
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    element.click()
    return element


def authenticated_page(driver):
    """Recognize EUserv's authenticated navigation without trusting a stored cookie alone."""
    if first_visible(
        driver,
        [
            (By.CSS_SELECTOR, "input[type='password']"),
            (By.NAME, "password"),
            (By.ID, "password"),
        ],
    ):
        return False
    if first_visible(
        driver,
        [
            (By.CSS_SELECTOR, "a[href*='logout' i]"),
            (By.CSS_SELECTOR, "a[href*='logoff' i]"),
        ],
    ):
        return True
    navigation_markers = {
        "contracts",
        "customer data",
        "customer account / invoices",
        "system messages",
        "support requests",
    }
    visible_markers = 0
    for element in driver.find_elements(By.CSS_SELECTOR, "a"):
        if element.is_displayed() and element_label(element) in navigation_markers:
            visible_markers += 1
    return visible_markers >= 2


def wait_for_authenticated_page(driver, timeout):
    try:
        WebDriverWait(driver, timeout).until(authenticated_page)
    except TimeoutException as error:
        raise RuntimeError("EUserv login did not reach an authenticated account page") from error


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
    if os.getenv("HEADLESS", "false").lower() == "true":
        raise RuntimeError("A visual CAPTCHA was detected in headless mode; manual login is required")
    if not sys.stdin.isatty():
        raise RuntimeError("A visual CAPTCHA needs an interactive terminal and manual browser access")
    print("A visual CAPTCHA is displayed. Complete it in the browser, then press Enter here.")
    input()


def fill(driver, selectors, value, timeout):
    element = WebDriverWait(driver, timeout).until(lambda d: first_visible(d, selectors))
    element.clear()
    element.send_keys(value)
    return element


def verification_code_box(driver, timeout):
    """Find EUserv login or renewal verification fields used by known UI variants."""
    return first_visible(
        driver,
        [
            (By.CSS_SELECTOR, "input[name='auth']"),
            (By.CSS_SELECTOR, "input[name='pin']"),
            (By.CSS_SELECTOR, "input[name*='code']"),
            (By.CSS_SELECTOR, "input[name*='otp']"),
            (By.CSS_SELECTOR, "input[autocomplete='one-time-code']"),
        ],
        timeout,
    )


def acquire_process_lock(path):
    """Hold a non-blocking process lock on Linux for the lifetime of the returned file."""
    if not sys.platform.startswith("linux"):
        return None
    import fcntl

    lock_file = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        raise RenewalFailure("Another EUserv renewal process is already running")
    return lock_file


def final_confirmation_button(driver, code_box, renewal_trigger, timeout):
    """Find the final submit control without reusing the initial renewal action."""
    excluded_ids = {renewal_trigger.id} if renewal_trigger is not None else set()
    specific_labels = [
        "confirm renewal",
        "renew now",
        "confirm extension",
        "extend contract now",
        "jetzt verlängern",
        "verlängerung bestätigen",
    ]
    form = None
    if code_box is not None:
        try:
            form = code_box.find_element(By.XPATH, "ancestor::form[1]")
        except Exception:
            pass

    def locate(current_driver):
        if form is not None:
            in_form = find_exact_clickable(
                form,
                specific_labels + ["confirm", "verify", "bestätigen", "continue", "weiter"],
                excluded_ids,
            )
            if in_form:
                return in_form
        return find_exact_clickable(current_driver, specific_labels, excluded_ids)

    return WebDriverWait(driver, timeout).until(locate)


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="EUserv renewal helper")
    parser.add_argument("--commit", action="store_true", help="allow the final renewal submission")
    parser.add_argument("--timeout", type=int, default=int(os.getenv("TIMEOUT_SECONDS", "25")))
    args = parser.parse_args()

    notification_attempted = False
    driver = None
    lock_file = None
    login_code = None

    def notify_once(message):
        nonlocal notification_attempted
        if notification_attempted:
            return
        notification_attempted = True
        notify_telegram(message)

    def handle_termination(signum, _frame):
        if args.commit:
            notify_once(
                "❌ EUserv automatic renewal was terminated before completion. "
                "No automatic retry will be made; please check the account and systemd log."
            )
        raise SystemExit(128 + signum)

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_termination)

    try:
        contract = os.getenv("EUSERV_CONTRACT_NUMBER", "").strip()
        if args.commit and not contract:
            raise RuntimeError("EUSERV_CONTRACT_NUMBER is required when --commit is used")
        lock_file = acquire_process_lock(os.getenv("LOCK_FILE", "/run/euserv-renewal.lock"))
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
        driver.set_page_load_timeout(int(os.getenv("PAGE_LOAD_TIMEOUT", "60")))
        try:
            driver.get(os.getenv("EUSERV_LOGIN_URL", "https://support.euserv.de/"))
        except TimeoutException:
            print("EUserv page load reached its time limit; validating the loaded page.")
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass

        login_selectors = [(By.NAME, "email"), (By.CSS_SELECTOR, "input[type='email']"), (By.NAME, "username"), (By.NAME, "login"), (By.ID, "username")]
        login_box = first_visible(driver, login_selectors, 5)
        if login_box:
            login_box.clear()
            login_box.send_keys(username)
            fill(driver, [(By.CSS_SELECTOR, "input[type='password']"), (By.NAME, "password"), (By.ID, "password")], password, args.timeout)
            wait_for_manual_captcha(driver, args.timeout)
            login_submitted_at = time.time()
            click_exact_by_text(driver, ["login", "anmelden", "sign in"], args.timeout)
            try:
                login_state = WebDriverWait(driver, args.timeout).until(
                    lambda current_driver: verification_code_box(current_driver, 0)
                    or ("authenticated" if authenticated_page(current_driver) else None)
                )
            except TimeoutException as error:
                raise RuntimeError("EUserv login appears to have failed") from error

            if not isinstance(login_state, str):
                code_box = login_state
                print("Waiting for EUserv login verification email…")
                login_subject = os.getenv(
                    "GMAIL_LOGIN_SUBJECT_HINT",
                ) or os.getenv("GMAIL_SUBJECT_HINT", "")
                login_code = wait_for_gmail_code(
                    login_submitted_at,
                    180,
                    subject_hint=login_subject,
                )
                code_box.send_keys(login_code)
                click_exact_by_text(
                    driver,
                    ["confirm", "verify", "bestätigen", "weiter", "continue"],
                    args.timeout,
                )
                wait_for_authenticated_page(driver, args.timeout)
        else:
            print("Reusing existing EUserv browser session; no login submitted.")
            if verification_code_box(driver, 0):
                raise RuntimeError(
                    "The saved browser session is already waiting for a verification PIN; "
                    "a fresh automatic login was not submitted"
                )
            wait_for_authenticated_page(driver, args.timeout)

        if contract:
            click_contract(driver, contract, args.timeout)
        print("Looking for renewal action…")
        renewal_labels = [
            "verlängern",
            "verlaengern",
            "vertrag verlängern",
            "renew",
            "renew now",
            "renew contract",
            "extend now",
            "extend contract",
            "extend contract now",
            "extension",
            "contract extension",
            "request extension",
        ]
        try:
            renewal_trigger = wait_for_exact_clickable(driver, renewal_labels, args.timeout)
        except TimeoutException:
            diagnostic = Path.cwd() / "no-renewal-action.png"
            try:
                driver.save_screenshot(str(diagnostic))
                print("Diagnostic screenshot saved to {}.".format(diagnostic))
            except Exception:
                pass
            print("No renewal action is currently available. The contract may already be renewed or not yet be inside its renewal window.")
            if args.commit:
                notify_once(
                    "⚠️ EUserv automatic renewal found no renewal action. "
                    "The contract may already be renewed or outside its renewal window. "
                    "No automatic retry will be made; please check the account."
                )
                raise RenewalFailure("No renewal action was available")
            return

        if not args.commit:
            print(
                "A renewal action is available. Dry-run mode did not click it, request a PIN, "
                "or submit any renewal."
            )
            return

        renewal_requested_at = time.time()
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", renewal_trigger)
        renewal_trigger.click()

        # If the renewal flow sends a second email code, fill it in.
        code_box = verification_code_box(driver, args.timeout)
        if code_box:
            print("Waiting for EUserv renewal verification email…")
            excluded = {login_code} if login_code else set()
            renewal_subject = os.getenv(
                "GMAIL_RENEWAL_SUBJECT_HINT",
            ) or os.getenv("GMAIL_SUBJECT_HINT", "")
            code_box.send_keys(
                wait_for_gmail_code(
                    renewal_requested_at,
                    180,
                    excluded,
                    subject_hint=renewal_subject,
                )
            )

        submitted_at = time.time()
        confirmation_button = final_confirmation_button(
            driver,
            code_box,
            renewal_trigger,
            args.timeout,
        )
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});",
            confirmation_button,
        )
        confirmation_button.click()
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
            notify_once("✅ EUserv contract renewal succeeded.")
        except TimeoutException:
            print("No recognized success message on the page; waiting for the EUserv confirmation email…")
            confirmation_timeout = int(os.getenv("GMAIL_CONFIRMATION_TIMEOUT", "300"))
            try:
                confirmed_contract, extended_until = wait_for_gmail_confirmation(
                    submitted_at, confirmation_timeout, contract
                )
                print(
                    "Renewal confirmed by email for contract {} until {}.".format(
                        confirmed_contract, extended_until
                    )
                )
                notify_once(
                    "✅ EUserv contract {} renewed until {} (confirmed by email).".format(
                        confirmed_contract, extended_until
                    )
                )
            except TimeoutException:
                print("Renewal was submitted, but neither the page nor a new confirmation email proved success.")
                notify_once(
                    "❌ EUserv renewal was submitted, but neither the website nor a new confirmation email "
                    "proved success. No automatic retry will be made; please check the account and systemd log."
                )
                raise RenewalFailure("Renewal submission could not be verified")
    except Exception as error:
        diagnostic = Path.cwd() / "failure.png"
        if driver is not None:
            try:
                driver.save_screenshot(str(diagnostic))
                print("Failure screenshot saved to {}.".format(diagnostic), file=sys.stderr)
            except Exception:
                pass
        if args.commit and not notification_attempted:
            detail = str(error).splitlines()[0][:240] or error.__class__.__name__
            notify_once(
                "❌ EUserv automatic renewal failed: {}. "
                "No automatic retry will be made; please check the systemd log.".format(detail)
            )
        raise
    finally:
        if driver is not None:
            if os.getenv("HEADLESS", "false").lower() == "true":
                try:
                    driver.quit()
                except Exception as error:
                    print("Could not close Chromium cleanly: {}".format(error), file=sys.stderr)
            else:
                print("Browser left open for review. Close it when finished.")
        if lock_file is not None:
            lock_file.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("ERROR [{}]: {}".format(type(error).__name__, error), file=sys.stderr)
        sys.exit(1)
