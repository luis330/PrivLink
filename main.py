"""兼容入口：保留 `uvicorn main:app`、`python main.py` 与 `import main` 的旧用法。"""

from privlink.app import app
from privlink.cli import main

__all__ = ["app", "main"]


if __name__ == "__main__":
    main()
