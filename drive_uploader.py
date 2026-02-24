"""
Google Drive uploader.
Creates folders and uploads tailored resumes + cover letters to Google Drive.

Setup:
1. Go to https://console.cloud.google.com
2. Create a project (or use existing)
3. Enable "Google Drive API"
4. Create a Service Account → download the JSON key file
5. Share your target Google Drive folder with the service account email
   (the email looks like: name@project.iam.gserviceaccount.com)
6. Set the path to the JSON key file in config.yaml as `gdrive_credentials_path`

Alternative: OAuth2 flow (for personal Drive, not shared drives)
- Create OAuth2 credentials instead of service account
- Set `gdrive_auth_type: oauth` in config.yaml
"""

import re
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


SCOPES = ['https://www.googleapis.com/auth/drive.file']


def _get_drive_service(credentials_path: str):
    """Authenticate and return a Google Drive API service."""
    creds = service_account.Credentials.from_service_account_file(
        credentials_path, scopes=SCOPES
    )
    return build('drive', 'v3', credentials=creds)


def _sanitize_folder_name(text: str) -> str:
    """Make a string safe for use as a folder name."""
    return re.sub(r'[^\w\s-]', '', text).strip().replace(' ', '_')[:80]


def _find_or_create_folder(service, folder_name: str, parent_id: str) -> str:
    """
    Find an existing folder by name under parent, or create it.
    Returns the folder ID.
    """
    # Search for existing folder
    query = (
        f"name = '{folder_name}' "
        f"and mimeType = 'application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents "
        f"and trashed = false"
    )
    results = service.files().list(
        q=query, spaces='drive', fields='files(id, name)', pageSize=1
    ).execute()

    files = results.get('files', [])
    if files:
        return files[0]['id']

    # Create new folder
    folder_metadata = {
        'name': folder_name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [parent_id],
    }
    folder = service.files().create(
        body=folder_metadata, fields='id'
    ).execute()

    return folder['id']


def upload_to_drive(
    credentials_path: str,
    parent_folder_id: str,
    company: str,
    job_id: str,
    resume_path: str,
    cover_letter_path: str,
) -> dict:
    """
    Upload tailored resume and cover letter to Google Drive.

    Creates a subfolder named <company>_<job_id> inside the parent folder,
    then uploads both documents into it.

    Args:
        credentials_path:   Path to Google service account JSON key
        parent_folder_id:   Google Drive folder ID to create subfolders in
        company:            Company name (used in folder name)
        job_id:             Job identifier (used in folder name)
        resume_path:        Path to the tailored resume .docx
        cover_letter_path:  Path to the cover letter .docx

    Returns:
        {
            "folder_id": str,
            "folder_name": str,
            "resume_file_id": str,
            "cover_letter_file_id": str,
            "folder_link": str,
            "error": str | None
        }
    """
    try:
        service = _get_drive_service(credentials_path)

        # Create subfolder: <company>_<job_id>
        safe_company = _sanitize_folder_name(company)
        safe_job_id = _sanitize_folder_name(job_id)
        folder_name = f"{safe_company}_{safe_job_id}"

        folder_id = _find_or_create_folder(service, folder_name, parent_folder_id)

        # Upload resume
        resume_file_id = _upload_file(
            service, resume_path, f"Tailored_Resume_{safe_company}.docx", folder_id
        )

        # Upload cover letter
        cl_file_id = _upload_file(
            service, cover_letter_path, f"Cover_Letter_{safe_company}.docx", folder_id
        )

        folder_link = f"https://drive.google.com/drive/folders/{folder_id}"

        return {
            "folder_id": folder_id,
            "folder_name": folder_name,
            "resume_file_id": resume_file_id,
            "cover_letter_file_id": cl_file_id,
            "folder_link": folder_link,
            "error": None,
        }

    except Exception as e:
        return {
            "folder_id": None,
            "folder_name": None,
            "resume_file_id": None,
            "cover_letter_file_id": None,
            "folder_link": None,
            "error": str(e),
        }


def _upload_file(service, local_path: str, drive_filename: str, parent_folder_id: str) -> str:
    """Upload a single file to Google Drive. Returns the file ID."""
    file_metadata = {
        'name': drive_filename,
        'parents': [parent_folder_id],
    }

    media = MediaFileUpload(
        local_path,
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        resumable=True,
    )

    file = service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id',
    ).execute()

    return file['id']
