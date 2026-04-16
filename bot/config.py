import os
from dataclasses import dataclass


@dataclass
class Config:
    telegram_bot_token: str
    telegram_chat_id: int
    anthropic_api_key: str
    google_sheets_id: str
    google_sheet_gid: int
    google_service_account_path: str
    yandex_disk_token: str
    yadisk_base_folder: str = "/Чеки"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
            telegram_chat_id=int(os.environ["TELEGRAM_CHAT_ID"]),
            anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
            google_sheets_id=os.environ.get(
                "GOOGLE_SHEETS_ID",
                "14bZiqurDD9_tMJ6OiScWf4-TL2JQ2Wo6MeXyc_ti8KA",
            ),
            google_sheet_gid=int(os.environ.get("GOOGLE_SHEET_GID", "1701314176")),
            google_service_account_path=os.environ.get(
                "GOOGLE_SERVICE_ACCOUNT", "service_account.json"
            ),
            yandex_disk_token=os.environ["YANDEX_DISK_TOKEN"],
            yadisk_base_folder=os.environ.get("YADISK_BASE_FOLDER", "/Чеки"),
        )
