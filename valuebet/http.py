"""Közös HTTP-réteg böngészőszerű fejlécekkel.

A vegas.hu mögötti Altenar API csak akkor ad 200-at, ha a kérés valódi
böngészőnek néz ki (teljes User-Agent + sec-fetch-* + Accept-Encoding).
Enélkül 400 Bad Request a válasz.
"""
import time
import requests


BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "hu-HU,hu;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Origin": "https://vegas.hu",
    "Referer": "https://vegas.hu/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "cross-site",
}


class Http:
    def __init__(self, verify_ssl=True, delay_sec=0.3):
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)
        self.verify_ssl = verify_ssl
        self.delay_sec = delay_sec
        if not verify_ssl:
            requests.packages.urllib3.disable_warnings()

    def get_json(self, url, params=None, headers=None):
        if self.delay_sec:
            time.sleep(self.delay_sec)
        try:
            r = self.session.get(
                url, params=params, headers=headers,
                timeout=30, verify=self.verify_ssl,
            )
        except requests.exceptions.SSLError as e:
            raise RuntimeError(
                "SSL hiba. Ha céges hálózaton/vírusirtó-proxy mögött vagy, "
                'állítsd a config.json-ban: "http": { "verify_ssl": false }.\n'
                f"Eredeti hiba: {e}"
            )
        r.raise_for_status()
        return r.json()

    def get_text(self, url, params=None, headers=None):
        """Nyers szöveg (HTML) letöltése – pl. eredmény-oldalak scrapeléséhez."""
        if self.delay_sec:
            time.sleep(self.delay_sec)
        try:
            r = self.session.get(
                url, params=params, headers=headers,
                timeout=30, verify=self.verify_ssl,
            )
        except requests.exceptions.SSLError as e:
            raise RuntimeError(
                "SSL hiba. Ha céges hálózaton/vírusirtó-proxy mögött vagy, "
                'állítsd a config.json-ban: "http": { "verify_ssl": false }.\n'
                f"Eredeti hiba: {e}"
            )
        r.raise_for_status()
        return r.text
