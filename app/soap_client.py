"""SOAP web service client with outcome classification."""

import logging
import re
from dataclasses import dataclass
from xml.sax.saxutils import escape

import requests

log = logging.getLogger("fal.soap")

SUCCESS = "SUCCESS"
NOT_FOUND = "NOT_FOUND"
FAULT = "FAULT"
TRANSPORT_ERROR = "TRANSPORT_ERROR"

_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_FAULT_RE = re.compile(r"<(?:[\w.-]+:)?Fault[\s>]", re.I)
_FAULT_TEXT_RES = [
    re.compile(r"<faultstring[^>]*>(.*?)</faultstring>", re.I | re.S),
    re.compile(r"<(?:[\w.-]+:)?Text[^>]*>(.*?)</(?:[\w.-]+:)?Text>", re.I | re.S),
]
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class SoapResult:
    outcome: str
    http_status: int = None
    message: str = ""
    body: str = ""


class SoapClient:
    def __init__(self, cfg):
        c = cfg.section("soap")
        self.url = c["endpoint_url"]
        self.version = str(c.get("soap_version", "1.1"))
        self.action = c.get("soap_action", "")
        self.timeout = float(c.get("timeout_seconds", 60))
        self.template = c["envelope_template"]
        self.excerpt_chars = int(c.get("response_excerpt_chars", 2000))
        self.not_found = [re.compile(p, re.I) for p in c.get("not_found_patterns", [])]
        self.success = [re.compile(p, re.I) for p in c.get("success_patterns", [])]

        self._session = requests.Session()
        self._verify = c.get("ca_bundle") or bool(c.get("verify_ssl", True))
        headers = dict(c.get("headers", {}))
        if self.version == "1.2":
            ctype = "application/soap+xml; charset=utf-8"
            if self.action:
                ctype += f'; action="{self.action}"'
            headers.setdefault("Content-Type", ctype)
        else:
            headers.setdefault("Content-Type", "text/xml; charset=utf-8")
            headers.setdefault("SOAPAction", f'"{self.action}"')
        self._session.headers.update(headers)

        auth = c.get("auth", {})
        auth_type = auth.get("type", "none")
        if auth_type == "basic":
            self._session.auth = (auth.get("username", ""), auth.get("password", ""))
        elif auth_type == "bearer":
            self._session.headers["Authorization"] = f"Bearer {auth.get('token', '')}"

    # -- request building --------------------------------------------------
    def build_envelope(self, params):
        def repl(match):
            key = match.group(1)
            if key not in params:
                log.warning("Envelope placeholder '{{%s}}' has no value; using an empty string", key)
                return ""
            return escape(str(params[key]))

        return _PLACEHOLDER.sub(repl, self.template)

    # -- classification ----------------------------------------------------
    @staticmethod
    def _summarize(body):
        for rx in _FAULT_TEXT_RES:
            m = rx.search(body)
            if m:
                return " ".join(m.group(1).split())[:500]
        text = " ".join(_TAG_RE.sub(" ", body).split())
        return text[:500]

    def classify(self, status, body):
        message = self._summarize(body)
        if any(rx.search(body) for rx in self.not_found):
            return SoapResult(NOT_FOUND, status, message, body)
        has_fault = bool(_FAULT_RE.search(body))
        if self.success:
            ok = any(rx.search(body) for rx in self.success) and not has_fault
        else:
            ok = status is not None and 200 <= status < 300 and not has_fault
        if ok:
            return SoapResult(SUCCESS, status, message, body)
        return SoapResult(FAULT, status, message or f"HTTP {status}", body)

    # -- call --------------------------------------------------------------
    def call(self, file_name, extra=None):
        params = dict(extra or {})
        params["file_name"] = file_name
        envelope = self.build_envelope(params)
        try:
            response = self._session.post(
                self.url,
                data=envelope.encode("utf-8"),
                timeout=self.timeout,
                verify=self._verify,
            )
        except requests.RequestException as exc:
            return SoapResult(TRANSPORT_ERROR, None, str(exc)[:500], "")
        response.encoding = response.encoding or "utf-8"
        return self.classify(response.status_code, response.text)

    def excerpt(self, result):
        return (result.body or "")[: self.excerpt_chars]
