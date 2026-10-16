"""Allow `python -m qoder` as an alias for the HTTP sidecar entrypoint."""
from qoder.http_api.server import main

if __name__ == "__main__":
    main()
