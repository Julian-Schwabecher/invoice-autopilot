"""Start Invoice Autopilot and open it in your browser. Works the same on Windows, macOS and Linux."""
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))


def free_port(first: int = 8460, last: int = 8470) -> int | None:
    for port in range(first, last + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return None


def open_when_ready(port: int) -> None:
    for _ in range(100):  # up to ~20 seconds
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    else:
        return
    webbrowser.open(f"http://localhost:{port}")


def main() -> int:
    port = free_port()
    if port is None:
        print("Couldn't find a free port (8460 to 8470). Close other copies of Invoice Autopilot and try again.")
        return 1
    print(f"Invoice Autopilot is running at http://localhost:{port}  (close this window to stop it)")
    threading.Thread(target=open_when_ready, args=(port,), daemon=True).start()
    try:
        import uvicorn
        uvicorn.run("app.main:app", host="127.0.0.1", port=port, log_level="warning")
    except KeyboardInterrupt:
        print("Stopped. You can close this window.")
    except OSError as e:
        print(f"Couldn't start the app ({e.strerror or e}). Close other copies and try again.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
