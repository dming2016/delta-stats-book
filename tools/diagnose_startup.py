"""Exercise only local static HTTP startup; never query accounts or launch UI."""

import json
import argparse
import subprocess
import sys
import threading
import time
from http.client import HTTPConnection
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stats_server import create_server
from friend_client import start_local_server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recovery", action="store_true")
    args = parser.parse_args()
    if args.recovery:
        for _ in range(60):
            server, thread = start_local_server()
            server.shutdown()
            server.server_close()
            thread.join()
        print(json.dumps({"iterations": 60, "recovery": True, "failures": []}))
        return 0
    failures = []
    for iteration in range(60):
        server = create_server(port=0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors = []
        deadline = time.monotonic() + 10
        try:
            while time.monotonic() < deadline:
                connection = HTTPConnection("127.0.0.1", port, timeout=0.5)
                try:
                    connection.request("GET", "/index.html", headers={"Connection": "close"})
                    response = connection.getresponse()
                    body = response.read()
                    if response.status == 200 and body:
                        break
                    errors.append({"status": response.status, "length": len(body)})
                except OSError as error:
                    errors.append({"error": type(error).__name__, "detail": str(error)})
                    time.sleep(0.1)
                finally:
                    connection.close()
            else:
                failures.append({"iteration": iteration, "port": port, "errors": errors})
                netstat = subprocess.run(
                    ["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                print(json.dumps({"port": port, "sockets": [
                    line.strip() for line in netstat.stdout.splitlines()
                    if f":{port} " in line
                ]}), flush=True)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        if errors:
            print(json.dumps({"iteration": iteration, "errors": errors}), flush=True)
    print(json.dumps({"iterations": 60, "failures": failures}), flush=True)
    return bool(failures)


if __name__ == "__main__":
    sys.exit(main())
