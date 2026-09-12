import socket

HOST = "127.0.0.1"
PORT = 50000


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((HOST, PORT))
    print(f"[client] connected to {HOST}:{PORT}")
    try:
        while True:
            try:
                line = input("You: ")
            except EOFError:
                break
            if not line:
                continue
            sock.sendall(line.encode("utf-8") + b"\n")
            data = sock.recv(4096)
            if not data:
                print("[client] server closed connection")
                break
            print(f"Echo: {data.decode('utf-8')}", end="")
    except ConnectionResetError:
        print("[client] connection reset by server")
    finally:
        sock.close()
        print("[client] disconnected")


if __name__ == "__main__":
    main()
