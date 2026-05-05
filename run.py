from app import create_app

app = create_app()

if __name__ == "__main__":
    import os

    host = os.getenv("CC_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port_raw = os.getenv("CC_PORT", "8000").strip() or "8000"
    port = int(port_raw) if port_raw.isdigit() else 8000
    app.run(host=host, port=port)
