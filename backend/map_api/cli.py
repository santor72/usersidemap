import os

import uvicorn


def main():
    uvicorn.run(
        "map_api.app:app",
        host=os.getenv("MAP_API_HOST", "127.0.0.1"),
        port=int(os.getenv("MAP_API_PORT", "8000")),
        access_log=False,  # XYZ query strings contain HMAC credentials.
    )


if __name__ == "__main__":
    main()
