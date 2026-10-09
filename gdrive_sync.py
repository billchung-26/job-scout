"""Overwrite fixed Google Drive files with the freshly generated local ones (run by daily.yml).

Why update-in-place instead of delete+create: the file keeps the same ID and share link, and a
Drive file the service account did not create is never counted against the account's own quota
(service accounts have none on a personal Drive, so *creating* files as one fails with 403).
That is also why the target Google Sheet is created once by hand/by Claude, then only updated.

Config: gdrive.yaml  (file IDs are identifiers, not secrets, so they live in git).
Auth:   env GDRIVE_SA_JSON = the service-account key JSON (GitHub Actions secret).
        The Sheet must be shared with the service account's e-mail as Editor. Share only that
        one file, not the whole folder: the scope below is full `drive` (drive.file cannot see
        files someone else created), and the share list is what actually limits the blast radius.

Uploading text/csv onto a Google Sheet makes Drive convert and replace the whole content.
Ceiling: Sheets caps at 10M cells and the media upload here is a single request (fine up to a few
MB; latest.csv is ~75 KB). Past ~5 MB switch to a resumable upload.
"""
import json
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SCOPES = ["https://www.googleapis.com/auth/drive"]
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files/%s?uploadType=media&supportsAllDrives=true"
META_URL = "https://www.googleapis.com/drive/v3/files/%s?fields=name,mimeType,modifiedTime,trashed&supportsAllDrives=true"


def _session():
    raw = os.environ.get("GDRIVE_SA_JSON", "").strip()
    if not raw:
        sys.exit("gdrive_sync: secret GDRIVE_SA_JSON is empty or missing, so nothing was uploaded. "
                 "Add the service-account key JSON as a repository secret with that exact name "
                 "(GitHub -> Settings -> Secrets and variables -> Actions), then re-run the workflow.")
    try:
        info = json.loads(raw)
    except ValueError:
        sys.exit("gdrive_sync: GDRIVE_SA_JSON is not valid JSON. Paste the whole downloaded key file, "
                 "including the outer { }, into the secret.")
    # Imported late so the dependency is only needed where uploads actually happen.
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    print("gdrive_sync: authenticating as %s" % info.get("client_email", "?"))
    return AuthorizedSession(creds)


def _explain(resp, local, fid):
    if resp.status_code == 404:
        return ("Drive says file %s does not exist for this service account. Either the ID in gdrive.yaml "
                "is wrong or the file was not shared with the service account's e-mail as Editor." % fid)
    if resp.status_code == 403:
        return ("Drive refused the write (403). The service account can see the file but cannot edit it, "
                "or the Drive API is not enabled in its Google Cloud project. Body: %s" % resp.text[:300])
    return "Drive answered HTTP %d: %s" % (resp.status_code, resp.text[:300])


def upload(session, local, fid, mime):
    path = os.path.join(HERE, local)
    if not os.path.exists(path):
        sys.exit("gdrive_sync: %s does not exist, so there is nothing to upload. Did scout.py fail earlier "
                 "in the same run?" % local)
    with open(path, "rb") as f:
        body = f.read()
    r = session.patch(UPLOAD_URL % fid, data=body, headers={"Content-Type": mime})
    if r.status_code != 200:
        sys.exit("gdrive_sync: upload of %s failed. %s" % (local, _explain(r, local, fid)))
    # Re-read metadata: a 200 on the upload alone would not show that the right (untrashed) file changed.
    m = session.get(META_URL % fid)
    meta = m.json() if m.status_code == 200 else {}
    if meta.get("trashed"):
        sys.exit("gdrive_sync: %s uploaded, but the target Drive file is in the trash. Restore it or put a "
                 "new file ID in gdrive.yaml." % local)
    print("gdrive_sync: %s -> '%s' (%d bytes, modified %s)" % (local, meta.get("name", fid), len(body), meta.get("modifiedTime", "?")))


def main():
    with open(os.path.join(HERE, "gdrive.yaml")) as f:
        cfg = yaml.safe_load(f) or {}
    files = cfg.get("files") or []
    if not files:
        sys.exit("gdrive_sync: gdrive.yaml lists no files.")
    session = _session()
    for item in files:
        upload(session, item["local"], item["drive_file_id"], item.get("mime", "text/csv"))


if __name__ == "__main__":
    main()
