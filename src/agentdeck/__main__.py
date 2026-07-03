"""``python -m agentdeck`` / the ``agentdeck`` console script."""

from __future__ import annotations

import logging

import uvicorn

from .app import create_app
from .config import config_path, load_config


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Never emit bearer tokens even if something tries to log one.
    _install_redaction_filter()

    path = config_path()
    config = load_config(path)
    app = create_app(config)
    logging.getLogger(__name__).info(
        "agentdeck starting on %s:%d (%d account(s))",
        config.server.bind,
        config.server.port,
        len(config.accounts),
    )
    uvicorn.run(app, host=config.server.bind, port=config.server.port, log_level="info")


def _install_redaction_filter() -> None:
    import re

    pattern = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+|sk-ant-[A-Za-z0-9._\-]+")

    class _Redact(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            if isinstance(record.msg, str):
                record.msg = pattern.sub(r"\g<1>[redacted]", record.msg)
            return True

    logging.getLogger().addFilter(_Redact())


if __name__ == "__main__":
    main()
