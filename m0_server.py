import socket
import signal
import sys

HOST = "127.0.0.1"
PORT = 50000


def main():
    with socket.create_server((HOST, PORT)) as server:
        print(f"[server] listening on {HOST}:{PORT}")
        try:
            while True:
                conn, addr = server.accept()
                print(f"[server] client connected: {addr}")
                try:
                    while True:
                        data = conn.recv(4096)
                        if not data:
                            break
                        conn.sendall(data)
                except ConnectionResetError:
                    pass
                finally:
                    conn.close()
                    print(f"[server] client disconnected: {addr}")
        except KeyboardInterrupt:
            print("\n[server] shutting down")


if __name__ == "__main__":
    main()
