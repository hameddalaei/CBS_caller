"""SOAP web service client with outcome classification."""

import logging
import re
from dataclasses import dataclass
from uuid import uuid4
from xml.etree import ElementTree as ET
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
        self.params = dict(c.get("params", {}))
        self.result_code_path = c.get("result_code_path", "")
        self.result_desc_path = c.get("result_desc_path", "")
        self.success_codes = {str(value) for value in c.get("success_codes", ["0"])}

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
            if key not in params or params[key] is None or not str(params[key]).strip():
                raise ValueError(f"Missing SOAP parameter: {key}")
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
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return SoapResult(FAULT, status, "Invalid SOAP XML response", body)
        namespace = ("http://www.w3.org/2003/05/soap-envelope" if self.version == "1.2"
                     else "http://schemas.xmlsoap.org/soap/envelope/")
        soap_body = root.find(f"{{{namespace}}}Body")
        if root.tag != f"{{{namespace}}}Envelope" or soap_body is None:
            return SoapResult(FAULT, status, "Response is not a SOAP envelope with a Body", body)
        if any(rx.search(body) for rx in self.not_found):
            return SoapResult(NOT_FOUND, status, message, body)
        if status is None or not 200 <= status < 300 or soap_body.find(f"{{{namespace}}}Fault") is not None:
            return SoapResult(FAULT, status, message or f"HTTP {status}", body)
        if self.result_code_path:
            codes = soap_body.findall(self.result_code_path)
            if len(codes) != 1 or not (codes[0].text or "").strip():
                return SoapResult(FAULT, status, "Missing or ambiguous SOAP ResultCode", body)
            code = codes[0].text.strip()
            description = soap_body.findtext(self.result_desc_path, "") if self.result_desc_path else ""
            message = f"ResultCode={code}: {description.strip()}"[:500]
            if code not in self.success_codes:
                return SoapResult(FAULT, status, message, body)
        if self.success and not any(rx.search(body) for rx in self.success):
            return SoapResult(FAULT, status, "SOAP success pattern did not match", body)
        return SoapResult(SUCCESS, status, message, body)

    # -- call --------------------------------------------------------------
    def call(self, file_name, extra=None):
        params = dict(self.params)
        params.update(extra or {})
        params.setdefault("message_seq", uuid4().hex)
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
