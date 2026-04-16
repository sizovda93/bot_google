import os
from dataclasses import dataclass


@dataclass
class Config:
    telegram_bot_token: str
    telegram_chat_id: int
    openai_api_key: str
    openai_base_url: str
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
            openai_api_key=os.environ["OPENAI_API_KEY"],
            openai_base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
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
