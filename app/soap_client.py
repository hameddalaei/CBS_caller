"""
Huawei CBS SOAP client for BatchScatteredChgSubBasicInfo operations.
Constructs the correct Huawei SOAP envelope and classifies responses
based on <cbs:ResultCode>.
"""

import uuid
import logging
from typing import Optional

from zeep import Client
from zeep.transports import Transport
from zeep.exceptions import Fault
from requests import Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import get_config

logger = logging.getLogger(__name__)


def _make_transport(timeout: int) -> Transport:
    session = Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return Transport(session=session, timeout=timeout)


class SoapClientError(Exception):
    """Raised when the CBS SOAP call returns a non-zero ResultCode or a Fault."""


class SOAPClient:
    """
    Wraps the Huawei CBS BatchProvision SOAP service.

    Usage:
        client = SOAPClient()
        result_code, result_desc = client.submit_batch_file("CHGSCATTEREDACCT_20241001.txt")
    """

    def __init__(self) -> None:
        cfg = get_config()
        self._soap_cfg = cfg["soap"]
        self._header_cfg = self._soap_cfg["request_header"]
        timeout = int(self._soap_cfg.get("timeout", 30))
        self._client = Client(
            wsdl=self._soap_cfg["wsdl_url"],
            transport=_make_transport(timeout),
        )
        self._namespace = self._soap_cfg["namespace"]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit_batch_file(self, file_name: str) -> tuple[str, str]:
        """
        Send a BatchScatteredChgSubBasicInfo request for *file_name*.

        Returns:
            (result_code, result_desc) where result_code == "0" means the
            batch was accepted by CBS (record-level errors appear in the
            FAILED.msg file on NFS, not here).

        Raises:
            SoapClientError: on SOAP Fault or non-zero ResultCode.
        """
        msg_seq = str(uuid.uuid4()).replace("-", "")[:16].upper()
        envelope = self._build_envelope(file_name, msg_seq)

        logger.info(
            "Submitting SOAP request",
            extra={"file_name": file_name, "MessageSeq": msg_seq},
        )

        try:
            response = self._client.service.BatchScatteredChgSubBasicInfo(
                **envelope
            )
        except Fault as exc:
            raise SoapClientError(
                f"SOAP Fault for {file_name}: {exc.message}"
            ) from exc

        return self._parse_response(response, file_name)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_envelope(self, file_name: str, msg_seq: str) -> dict:
        """
        Build the kwargs dict that zeep maps onto:

          <BatchScatteredChgSubBasicInfoRequestMsg>
            <RequestHeader>
              <Version>...</Version>
              <BusinessCode>...</BusinessCode>
              <MessageSeq>...</MessageSeq>
              <BEID>...</BEID>
              <LoginSystemCode>...</LoginSystemCode>
              <Password>...</Password>
              <OperatorID>...</OperatorID>
            </RequestHeader>
            <BatchScatteredChgSubBasicInfoRequest>
              <FileName>...</FileName>
            </BatchScatteredChgSubBasicInfoRequest>
          </BatchScatteredChgSubBasicInfoRequestMsg>
        """
        header = self._header_cfg
        return {
            "RequestHeader": {
                "Version": header["Version"],
                "BusinessCode": header["BusinessCode"],
                "MessageSeq": msg_seq,
                "BEID": header["BEID"],
                "LoginSystemCode": header["LoginSystemCode"],
                "Password": header["Password"],
                "OperatorID": header["OperatorID"],
            },
            "BatchScatteredChgSubBasicInfoRequest": {
                "FileName": file_name,
            },
        }

    def _parse_response(
        self, response: object, file_name: str
    ) -> tuple[str, str]:
        """
        Extract ResultCode and ResultDesc from the CBS response object.

        CBS returns a generic confirmation here; record-level failures are
        written to CHGSCATTEREDACCT_*_FAILED.msg on the remote NFS share.
        A ResultCode of "0" means the batch was accepted.
        """
        try:
            # zeep deserialises the response; attribute access is safe.
            result_code = str(response.ResultCode)
            result_desc = str(response.ResultDesc)
        except AttributeError as exc:
            raise SoapClientError(
                f"Unexpected response shape for {file_name}: {response!r}"
            ) from exc

        if result_code != "0":
            raise SoapClientError(
                f"CBS rejected batch '{file_name}': "
                f"ResultCode={result_code}, ResultDesc={result_desc}"
            )

        logger.info(
            "CBS accepted batch",
            extra={
                "file_name": file_name,
                "ResultCode": result_code,
                "ResultDesc": result_desc,
            },
        )
        return result_code, result_desc
