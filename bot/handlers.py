"""Telegram message handlers: receive receipts and process them."""

import logging
import tempfile
from pathlib import Path

from aiogram import Bot, Router, F
from aiogram.types import Message

from bot.config import Config
from bot.receipt_parser import parse_receipt
from bot.sheets import SheetsClient
from bot.yadisk_client import YaDiskClient

logger = logging.getLogger(__name__)
router = Router()

# Will be initialized in main.py
config: Config
sheets: SheetsClient
yadisk_client: YaDiskClient


def init_services(cfg: Config) -> None:
    """Initialize shared services. Called once at startup."""
    global config, sheets, yadisk_client
    config = cfg
    sheets = SheetsClient(
        service_account_path=cfg.google_service_account_path,
        spreadsheet_id=cfg.google_sheets_id,
        gid=cfg.google_sheet_gid,
    )
    yadisk_client = YaDiskClient(
        token=cfg.yandex_disk_token,
        base_folder=cfg.yadisk_base_folder,
    )


def _extract_partner_from_caption(caption: str | None) -> str | None:
    """Extract partner name from message caption."""
    if not caption:
        return None
    # Caption IS the partner name (manager writes it when sending the receipt)
    # Strip and take the first line if multiline
    partner = caption.strip().split("\n")[0].strip()
    return partner if partner else None


def _make_target_filename(debtor_fio: str, original_ext: str) -> str:
    """Create target filename from debtor FIO."""
    safe_name = debtor_fio.strip()
    # Remove characters that are problematic in filenames
    for ch in r'<>:"/\|?*':
        safe_name = safe_name.replace(ch, "")
    return f"{safe_name}{original_ext}"


@router.message(F.document)
async def handle_document(message: Message, bot: Bot) -> None:
    """Handle incoming PDF documents."""
    doc = message.document
    if not doc or not doc.file_name:
        return

    ext = Path(doc.file_name).suffix.lower()
    if ext not in (".pdf", ".jpg", ".jpeg", ".png", ".webp"):
        return

    await _process_receipt(message, bot, ext)


@router.message(F.photo)
async def handle_photo(message: Message, bot: Bot) -> None:
    """Handle incoming photos (receipt snapshots)."""
    if not message.photo:
        return
    await _process_receipt(message, bot, ".jpg")


async def _process_receipt(message: Message, bot: Bot, ext: str) -> None:
    """Main processing pipeline for a receipt."""
    # Step 1: Extract partner from caption
    partner = _extract_partner_from_caption(message.caption)
    if not partner:
        await message.reply(
            "⚠️ Укажите имя партнёра в подписи к чеку.\n"
            "Пример: отправьте файл с подписью «Давид Ростов»"
        )
        return

    # Step 2: Download file
    processing_msg = await message.reply("⏳ Обрабатываю чек...")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir) / f"receipt{ext}"

            if message.document:
                file = await bot.get_file(message.document.file_id)
            else:
                # Get largest photo
                photo = message.photo[-1]
                file = await bot.get_file(photo.file_id)
                ext = ".jpg"
                tmp_path = Path(tmpdir) / "receipt.jpg"

            await bot.download_file(file.file_path, tmp_path)
            logger.info("Downloaded file: %s (%d bytes)", tmp_path.name, tmp_path.stat().st_size)

            # Step 3: Parse receipt with Claude
            receipt = await parse_receipt(tmp_path, config.anthropic_api_key)
            logger.info("Parsed receipt: %s", receipt)

            if not receipt.debtor_fio:
                await processing_msg.edit_text(
                    "❌ Не удалось распознать ФИО должника из чека.\n"
                    "Проверьте качество файла или добавьте данные вручную."
                )
                return

            if not receipt.amount:
                await processing_msg.edit_text(
                    f"❌ Распознал должника: **{receipt.debtor_fio}**, "
                    "но не смог определить сумму.\n"
                    "Проверьте чек вручную.",
                    parse_mode="Markdown",
                )
                return

            # Step 4: Find debtor in Google Sheet
            match = sheets.find_debtor_row(receipt.debtor_fio)
            if not match:
                await processing_msg.edit_text(
                    f"❌ Не нашёл «{receipt.debtor_fio}» в таблице.\n"
                    f"Сумма: {receipt.amount:,.0f} ₽\n"
                    f"Партнёр: {partner}\n\n"
                    "Проверьте написание ФИО или добавьте строку в таблицу вручную."
                )
                return

            row_num, row_data = match

            # Step 5: Upload to Yandex Disk
            target_filename = _make_target_filename(receipt.debtor_fio, ext)
            public_url = yadisk_client.upload_and_share(
                local_path=tmp_path,
                partner_name=partner,
                target_filename=target_filename,
            )

            # Step 6: Update Google Sheet
            sheets.update_payment(
                row_num=row_num,
                amount=receipt.amount,
                check_link=public_url,
            )

            # Step 7: Confirm in chat
            amount_formatted = f"{receipt.amount:,.0f}".replace(",", " ")
            await processing_msg.edit_text(
                f"✅ Чек обработан!\n\n"
                f"👤 Должник: **{row_data['fio']}**\n"
                f"💰 Сумма: {amount_formatted} ₽\n"
                f"🏢 Партнёр: {partner}\n"
                f"📅 Дата: {receipt.date or 'не определена'}\n"
                f"🔗 [Ссылка на чек]({public_url})\n\n"
                f"Таблица обновлена (строка {row_num}).",
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            logger.info(
                "Successfully processed receipt: %s, %s RUB, partner=%s",
                receipt.debtor_fio,
                receipt.amount,
                partner,
            )

    except Exception as e:
        logger.exception("Error processing receipt")
        await processing_msg.edit_text(
            f"❌ Ошибка при обработке чека:\n`{e}`\n\nПопробуйте ещё раз или обработайте вручную.",
            parse_mode="Markdown",
        )
