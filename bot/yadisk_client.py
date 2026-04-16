"""Yandex Disk integration: upload files and get public share links."""

import logging
from pathlib import Path

import yadisk

logger = logging.getLogger(__name__)


class YaDiskClient:
    def __init__(self, token: str, base_folder: str = "/Чеки"):
        self.client = yadisk.Client(token=token)
        self.base_folder = base_folder.rstrip("/")
        self._ensure_base_folder()

    def _ensure_base_folder(self) -> None:
        """Create base folder if it doesn't exist."""
        if not self.client.exists(self.base_folder):
            self.client.mkdir(self.base_folder)
            logger.info("Created base folder: %s", self.base_folder)

    def _ensure_partner_folder(self, partner_name: str) -> str:
        """Create partner subfolder. Returns the full path."""
        folder_path = f"{self.base_folder}/{partner_name}"
        if not self.client.exists(folder_path):
            self.client.mkdir(folder_path)
            logger.info("Created partner folder: %s", folder_path)
        return folder_path

    def upload_and_share(
        self, local_path: Path, partner_name: str, target_filename: str
    ) -> str:
        """
        Upload file to Yandex Disk and return public link.

        Args:
            local_path: Local file path
            partner_name: Partner name (used as subfolder)
            target_filename: Desired filename on disk (e.g. "Иванов Иван Иванович.pdf")

        Returns:
            Public URL for the uploaded file
        """
        folder_path = self._ensure_partner_folder(partner_name)
        remote_path = f"{folder_path}/{target_filename}"

        # Handle name collisions: append (2), (3), etc.
        if self.client.exists(remote_path):
            stem = Path(target_filename).stem
            suffix = Path(target_filename).suffix
            counter = 2
            while self.client.exists(remote_path):
                remote_path = f"{folder_path}/{stem} ({counter}){suffix}"
                counter += 1
            logger.warning("Name collision, using: %s", remote_path)

        self.client.upload(str(local_path), remote_path)
        logger.info("Uploaded: %s → %s", local_path.name, remote_path)

        # Publish and get public link
        self.client.publish(remote_path)
        info = self.client.get_meta(remote_path)
        public_url = info.public_url

        if not public_url:
            raise RuntimeError(f"Failed to get public URL for {remote_path}")

        logger.info("Public URL: %s", public_url)
        return public_url
