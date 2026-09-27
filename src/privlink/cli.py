from __future__ import annotations

from privlink import config
from privlink.db import init_storage


def main() -> None:
    import uvicorn

    init_storage()
    uvicorn.run("privlink.app:app", host=config.APP_HOST, port=config.APP_PORT, workers=1)
