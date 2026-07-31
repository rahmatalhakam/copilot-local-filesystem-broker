from __future__ import annotations

import uvicorn

from app.main import CONFIG, app


def main() -> None:
    uvicorn.run(
        app,
        host=CONFIG.host,
        port=CONFIG.port,
    )


if __name__ == '__main__':
    main()
