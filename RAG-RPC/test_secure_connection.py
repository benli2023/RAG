import os
from pathlib import Path
from remote_vector_database import RemoteVectorDatabase

def test_secure_connection():
    # Configuration: prioritize REMOTE_DB_TARGET, fall back to localhost + RPC_PORT
    target = os.getenv("REMOTE_DB_TARGET", "")
    if not target:
        port = os.getenv("RPC_PORT", "50051")
        target = f"localhost:{port}"
    
    # If target is just a port, prepend localhost
    if ":" not in target:
        target = f"localhost:{target}"
        
    cert_path = Path(__file__).resolve().parent / "certs" / "server.crt"
    collection_name = "test_collection"

    print(f"Target: {target}")
    print(f"Cert Path: {cert_path}")

    if not cert_path.exists():
        print(f"Error: Certificate file not found at {cert_path}")
        return

    try:
        # Initialize client with TLS
        db = RemoteVectorDatabase(target, str(cert_path), collection_name)
        
        # Test connection by calling count()
        print("Connecting to server...")
        count = db.count()
        print(f"Success! Connected to secure gRPC server. Current record count: {count}")
        
    except Exception as e:
        print(f"Failed to connect to secure gRPC server: {e}")
        print("Make sure the server is running with 'python server.py'")

if __name__ == "__main__":
    test_secure_connection()
