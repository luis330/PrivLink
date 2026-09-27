from privlink.app import app

import asgi

Default = asgi.entrypoint(app)
