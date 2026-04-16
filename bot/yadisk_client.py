"""Yandex Disk integration: upload files and get public share links."""

import logging
from pathlib import Path
from typing import Optional, List

from thefuzz import fuzz
import yadisk

logger = logging.getLogger(__name__)

FOLDER_MATCH_THRESHOLD = 70


class YaDiskClient:
    def __init__(self, token: str, base_folder: str = "/Чеки"):
        self.client = yadisk.Client(token=token)
        self.base_folder = base_folder.rstrip("/")
        self._ensure_base_folder()
        self._folder_cache = None  # type: Optional[List[str]]

    def _ensure_base_folder(self) -> None:
        """Create base folder if it doesn't exist."""
        if not self.client.exists(self.base_folder):
            self.client.mkdir(self.base_folder)
            logger.info("Created base folder: %s", self.base_folder)

    def _list_partner_folders(self) -> List[str]:
        """List existing partner folder names in base folder. Cached."""
        if self._folder_cache is not None:
            return self._folder_cache

        folders = []
        try:
            for item in self.client.listdir(self.base_folder):
                if item.type == "dir":
                    folders.append(item.name)
        except Exception as e:
            logger.warning("Failed to list folders: %s", e)

        self._folder_cache = folders
        logger.info("Loaded %d partner folders from YaDisk", len(folders))
        return folders

    def _match_partner_folder(self, partner_name: str) -> str:
        """
        Match partner name against existing folders using fuzzy matching.
        Returns exact folder name if match found, otherwise sanitized partner_name.
        """
        # Sanitize: remove slashes and other path-breaking chars
        sanitized = partner_name.replace("/", " ").replace("\\", " ").strip()

        folders = self._list_partner_folders()
        if not folders:
            return sanitized

        best_folder = None
        best_score = 0

        for folder in folders:
            score_ratio = fuzz.ratio(sanitized.lower(), folder.lower())
            score_partial = fuzz.partial_ratio(sanitized.lower(), folder.lower())
            score_sort = fuzz.token_sort_ratio(sanitized.lower(), folder.lower())
            score = max(score_ratio, score_partial, score_sort)

            if score > best_score:
                best_score = score
                best_folder = folder

        if best_folder and best_score >= FOLDER_MATCH_THRESHOLD:
            logger.info(
                "Folder match: '%s' → '%s' (score=%d)",
                partner_name, best_folder, best_score,
            )
            return best_folder

        logger.warning(
            "No folder match for '%s' (best='%s', score=%d), using sanitized name",
            partner_name, best_folder, best_score,
        )
        return sanitized

    def _ensure_partner_folder(self, partner_name: str) -> str:
        """Find or create partner subfolder. Returns the full path."""
        matched_name = self._match_partner_folder(partner_name)
        folder_path = "{}/{}".format(self.base_folder, matched_name)

        if not self.client.exists(folder_path):
            self.client.mkdir(folder_path)
            # Invalidate cache so new folder is picked up
            self._folder_cache = None
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
            target_filename: Desired filename on disk

        Returns:
            Public URL for the uploaded file
        """
        # Sanitize filename too
        safe_filename = target_filename.replace("/", " ").replace("\\", " ")

        folder_path = self._ensure_partner_folder(partner_name)
        remote_path = "{}/{}".format(folder_path, safe_filename)

        # Handle name collisions: append (2), (3), etc.
        if self.client.exists(remote_path):
            stem = Path(safe_filename).stem
            suffix = Path(safe_filename).suffix
            counter = 2
            while self.client.exists(remote_path):
                remote_path = "{}/{} ({}){}".format(folder_path, stem, counter, suffix)
                counter += 1
            logger.warning("Name collision, using: %s", remote_path)

        self.client.upload(str(local_path), remote_path)
        logger.info("Uploaded: %s → %s", local_path.name, remote_path)

        # Publish and get public link
        self.client.publish(remote_path)
        info = self.client.get_meta(remote_path)
        public_url = info.public_url

        if not public_url:
            raise RuntimeError("Failed to get public URL for {}".format(remote_path))

        logger.info("Public URL: %s", public_url)
        return public_url
