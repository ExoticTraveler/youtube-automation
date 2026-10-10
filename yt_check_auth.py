#!/usr/bin/env python3
"""Explicit, interactive readonly OAuth repair. Never uploads a video."""
import argparse
import pickle
import shutil
from datetime import datetime
from yt_autoupload import CHECK_TOKEN, CHECK_SCOPES, CLIENT_SECRET, get_check_service
from google_auth_oauthlib.flow import InstalledAppFlow

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reauthorize", action="store_true")
    args = parser.parse_args()
    if args.reauthorize:
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), CHECK_SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent")
        if not creds.valid or not creds.has_scopes(CHECK_SCOPES):
            raise RuntimeError("Readonly authorization was not granted; existing token preserved")
        if CHECK_TOKEN.exists():
            backup = CHECK_TOKEN.with_name(CHECK_TOKEN.name + ".backup_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
            shutil.copy2(CHECK_TOKEN, backup)
            backup.chmod(0o600)
        tmp = CHECK_TOKEN.with_suffix(".pickle.tmp")
        with tmp.open("wb") as handle:
            pickle.dump(creds, handle)
        tmp.chmod(0o600)
        tmp.replace(CHECK_TOKEN)
    get_check_service()
    print("Readonly credentials OK. Pending projects will retry automatically.")

if __name__ == "__main__": main()
