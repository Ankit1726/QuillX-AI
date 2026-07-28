import os
from dotenv import load_dotenv
load_dotenv()

def get_database():
    db_url = os.getenv("DATABASE_URL")

    if not db_url:
        raise ValueError(
            "Database Error ⚠️"
        )

    if "sslmode" not in db_url:
        seperator = "&" if "?" in db_url else "?"
        db_url = f"{db_url}{seperator}sslmode=require"
    return db_url