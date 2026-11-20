from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import Generator

import uvicorn

from app.certs import ensure_https_certificates
from app.config import DATA_DIR, HUB_HOST, HUB_HTTPS_PORT, HUB_PORT


class _SecondaryServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


class _DualPrimaryServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, peers: list[uvicorn.Server]) -> None:
        super().__init__(config)
        self._peers = peers

    def handle_exit(self, sig: int, frame) -> None:  # type: ignore[override]
        for peer in self._peers:
            peer.should_exit = True
        super().handle_exit(sig, frame)


async def _run_dual(http_cfg: uvicorn.Config, https_cfg: uvicorn.Config) -> None:
    srv_https = _SecondaryServer(https_cfg)
    srv_http = _DualPrimaryServer(http_cfg, peers=[srv_https])
    await asyncio.gather(srv_http.serve(), srv_https.serve())


def main() -> None:
    log_file = DATA_DIR / "hub_server.log"

    # Under pythonw.exe (legacy silent background mode), sys.stdout and sys.stderr are None.
    # Redirect them to hub_server.log so uvicorn never crashes on stream.isatty().
    # Under python.exe started with CREATE_NO_WINDOW (start_hub_hidden.pyw / admin restart),
    # stdout points to the invisible headless console (or DEVNULL), so nothing is redirected.
    stdout_redirected_to_log = False
    if sys.stdout is None or sys.stderr is None:
        fh = open(log_file, "a", encoding="utf-8", buffering=1)
        sys.stdout = fh
        sys.stderr = fh
        stdout_redirected_to_log = True

    # If stdout already writes into hub_server.log, a StreamHandler would duplicate every line.
    log_handlers: list[logging.Handler] = [logging.FileHandler(log_file, encoding="utf-8")]
    if not stdout_redirected_to_log:
        # Headless console / NUL may use a legacy code page: never fail on emoji / Cyrillic.
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(Exception):
                stream.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
        log_handlers.append(logging.StreamHandler(sys.stdout))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=log_handlers,
    )
    logger = logging.getLogger("agy_hub.runner")

    if not stdout_redirected_to_log:
        # stderr may be DEVNULL (start_hub_hidden.pyw): keep fatal tracebacks in hub_server.log.
        def _log_uncaught(exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
            if issubclass(exc_type, KeyboardInterrupt):
                sys.__excepthook__(exc_type, exc, tb)
                return
            logger.critical("Uncaught exception", exc_info=(exc_type, exc, tb))

        sys.excepthook = _log_uncaught

    from app.main import app as fastapi_app

    http_cfg = uvicorn.Config(
        fastapi_app,
        host=HUB_HOST,
        port=HUB_PORT,
        log_config=None,
        lifespan="on",
        ws_ping_interval=25.0,
        ws_ping_timeout=30.0,
    )

    if HUB_HTTPS_PORT > 0:
        try:
            cert_file, key_file = ensure_https_certificates()
            https_cfg = uvicorn.Config(
                fastapi_app,
                host=HUB_HOST,
                port=HUB_HTTPS_PORT,
                ssl_certfile=str(cert_file),
                ssl_keyfile=str(key_file),
                log_config=None,
                lifespan="off",
                ws_ping_interval=25.0,
                ws_ping_timeout=30.0,
            )
            logger.info(
                "Starting dual HTTP (:%d) + HTTPS (:%d, voice-enabled for LAN/mobile) servers",
                HUB_PORT,
                HUB_HTTPS_PORT,
            )
            asyncio.run(_run_dual(http_cfg, https_cfg))
            return
        except Exception as exc:
            logger.exception("HTTPS dual-port init failed (%s); falling back to HTTP only", exc)

    uvicorn.Server(http_cfg).run()


if __name__ == "__main__":
    main()
