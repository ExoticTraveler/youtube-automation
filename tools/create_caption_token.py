#!/usr/bin/env python3

import pickle
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

BASE = Path.home() / "YT_QUEUE"
SECRET = BASE / "secrets" / "client_secret.json"
TOKEN = BASE / "secrets" / "token_caption.pickle"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.force-ssl"
]

flow = InstalledAppFlow.from_client_secrets_file(
    str(SECRET),
    SCOPES,
)

creds = flow.run_local_server(port=0)

with TOKEN.open("wb") as f:
    pickle.dump(creds, f)

print("CREATED:", TOKEN)
