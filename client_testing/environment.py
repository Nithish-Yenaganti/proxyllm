"""Load client settings from beside these scripts, regardless of working directory."""
from pathlib import Path
from dotenv import load_dotenv


def load_client_environment():
    # Explicitly exported shell settings retain precedence over the local file.
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
