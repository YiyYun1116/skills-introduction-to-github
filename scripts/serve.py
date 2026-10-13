#!/usr/bin/env python3
"""Start one application worker behind a hosting provider's HTTPS proxy."""
import os
from pathlib import Path
import sys
from typing import Mapping


def listen_port(environment: Mapping[str, str] | None = None) -> int:
    environment = os.environ if environment is None else environment
    raw = environment.get('PORT', environment.get('SIGNAL_ATLAS_PORT', '8000'))
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise ValueError('PORT / SIGNAL_ATLAS_PORT 必须是 1–65535 的整数') from None
    if not 1 <= port <= 65535:
        raise ValueError('PORT / SIGNAL_ATLAS_PORT 必须是 1–65535 的整数')
    return port


def main() -> None:
    try:
        port = listen_port()
    except ValueError as error:
        raise SystemExit(str(error)) from None
    # This also works when invoked by absolute path from another directory.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import uvicorn

    uvicorn.run(
        'app.main:app',
        host='0.0.0.0',
        port=port,
        workers=1,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get('FORWARDED_ALLOW_IPS', '127.0.0.1'),
        server_header=False,
    )


if __name__ == '__main__':
    main()
