from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CHECKPOINT_DB_PATH = DATA_DIR / "checkpoints.sqlite"


def get_database() -> str:
    """
    Returns the local SQLite file path used for the LangGraph
    checkpointer. Kept as a function (same name/shape as before) so
    backend/agent.py doesn't need to change how it calls this.
    """
    return str(CHECKPOINT_DB_PATH)
