from app import create_app
from app.db import get_db

app = create_app()

with app.app_context():
    db = get_db()
    db.execute("DELETE FROM service_checks")
    db.commit()
    print("Cleared service_checks table")
