"""Telegram message handlers: receive receipts and process them."""

import logging
import tempfile
from pathlib import Path
from typing import Optional, Dict

from aiogram import Bot, Router, F
from aiogram.types import Message

from bot.config import Config
from bot.receipt_parser import parse_receipt, parse_caption, CaptionData
from bot.sheets import SheetsClient
from bot.yadisk_client import YaDiskClient

logger = logging.getLogger(__name__)
router = Router()

# Will be initialized in main.py
config: Config
sheets: SheetsClient
yadisk_client: YaDiskClient

# Pending receipts waiting for partner name (message_id → receipt data)
pending_receipts: Dict[int, dict] = {}


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


def _make_target_filename(debtor_fio: str, original_ext: str) -> str:
    """Create target filename from debtor FIO."""
    safe_name = debtor_fio.strip()
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


@router.message(F.reply_to_message & F.text)
async def handle_reply_with_partner(message: Message, bot: Bot) -> None:
    """Handle reply with partner name for pending receipts."""
    if not message.reply_to_message:
        return

    reply_to_id = message.reply_to_message.message_id
    if reply_to_id not in pending_receipts:
        return

    partner = message.text.strip()
    if not partner:
        return

    data = pending_receipts.pop(reply_to_id)
    await _finish_processing(
        message=message,
        bot=bot,
        debtor_fio=data["debtor_fio"],
        amount=data["amount"],
        date=data["date"],
        partner=partner,
        tmp_path=Path(data["tmp_path"]),
        ext=data["ext"],
        processing_msg_id=data["processing_msg_id"],
    )


async def _process_receipt(message: Message, bot: Bot, ext: str) -> None:
    """Main processing pipeline for a receipt."""
    processing_msg = await message.reply("⏳ Обрабатываю чек...")

    try:
        # Step 1: Download file
        tmpdir = tempfile.mkdtemp()
        tmp_path = Path(tmpdir) / f"receipt{ext}"

        if message.document:
            file = await bot.get_file(message.document.file_id)
        else:
            photo = message.photo[-1]
            file = await bot.get_file(photo.file_id)
            ext = ".jpg"
            tmp_path = Path(tmpdir) / "receipt.jpg"

        await bot.download_file(file.file_path, tmp_path)
        logger.info("Downloaded: %s (%d bytes)", tmp_path.name, tmp_path.stat().st_size)

        # Step 2: Extract client FIO + partner from caption (via LLM)
        caption_data = CaptionData(client_fio=None, partner=None)
        if message.caption:
            caption_data = await parse_caption(
                message.caption, config.openai_api_key, config.openai_base_url
            )
            logger.info("Caption: fio=%s, partner=%s", caption_data.client_fio, caption_data.partner)

        # Step 3: Parse receipt (amount, date, and backup FIO)
        receipt = await parse_receipt(
            tmp_path, config.openai_api_key, config.openai_base_url
        )
        logger.info("Parsed receipt: %s", receipt)

        # Use caption FIO if available, fallback to receipt FIO
        debtor_fio = caption_data.client_fio or receipt.debtor_fio
        caption_partner = caption_data.partner

        if not debtor_fio:
            await processing_msg.edit_text(
                "❌ Не удалось определить ФИО клиента.\n"
                "Не нашёл ни в подписи к сообщению, ни в самом чеке.\n"
                "Проверьте качество файла или добавьте данные вручную."
            )
            return

        if not receipt.amount:
            await processing_msg.edit_text(
                "❌ Распознал клиента: *{}*, "
                "но не смог определить сумму.\n"
                "Проверьте чек вручную.".format(debtor_fio),
                parse_mode="Markdown",
            )
            return

        # Step 4: Find debtor in Google Sheet (partner hint for disambiguation)
        match = sheets.find_debtor_row(debtor_fio, partner_hint=caption_partner)

        if match:
            row_num, row_data = match
            partner = row_data["partner"]
            logger.info("Found in sheet: row %d, partner=%s", row_num, partner)

            # Partner found in table — go straight to upload
            await _finish_processing(
                message=message,
                bot=bot,
                debtor_fio=row_data["fio"],
                amount=receipt.amount,
                date=receipt.date,
                partner=partner,
                tmp_path=tmp_path,
                ext=ext,
                processing_msg_id=processing_msg.message_id,
            )
        else:
            # Client not in table — ask for partner name via reply
            amount_fmt = "{:,.0f}".format(receipt.amount).replace(",", " ")
            ask_msg = await processing_msg.edit_text(
                "⚠️ Не нашёл *{}* в таблице.\n"
                "Сумма: {} ₽\n\n"
                "Ответьте на это сообщение — укажите *имя партнёра*.".format(
                    debtor_fio, amount_fmt
                ),
                parse_mode="Markdown",
            )

            # Save pending receipt for when partner reply comes
            pending_receipts[ask_msg.message_id] = {
                "debtor_fio": debtor_fio,
                "amount": receipt.amount,
                "date": receipt.date,
                "tmp_path": str(tmp_path),
                "ext": ext,
                "processing_msg_id": ask_msg.message_id,
            }
            logger.info("Pending receipt for '%s', waiting for partner reply", debtor_fio)

    except Exception as e:
        logger.exception("Error processing receipt")
        await processing_msg.edit_text(
            "❌ Ошибка при обработке чека:\n`{}`\n\n"
            "Попробуйте ещё раз или обработайте вручную.".format(e),
            parse_mode="Markdown",
        )


async def _finish_processing(
    message: Message,
    bot: Bot,
    debtor_fio: str,
    amount: float,
    date: Optional[str],
    partner: str,
    tmp_path: Path,
    ext: str,
    processing_msg_id: int,
) -> None:
    """Upload to YaDisk, update sheet, confirm in chat."""
    try:
        # Step 5: Upload to Yandex Disk
        target_filename = _make_target_filename(debtor_fio, ext)
        public_url = yadisk_client.upload_and_share(
            local_path=tmp_path,
            partner_name=partner,
            target_filename=target_filename,
        )

        # Step 6: Update Google Sheet (if debtor exists in table)
        match = sheets.find_debtor_row(debtor_fio)
        if match:
            row_num, row_data = match
            sheets.update_payment(
                row_num=row_num,
                amount=amount,
                check_link=public_url,
            )
            sheet_status = "Таблица обновлена (строка {}).".format(row_num)
        else:
            sheet_status = "⚠️ Клиента нет в таблице — обновите вручную."

        # Step 7: Confirm in chat
        amount_fmt = "{:,.0f}".format(amount).replace(",", " ")
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=processing_msg_id,
            text=(
                "✅ Чек обработан!\n\n"
                "👤 Клиент: *{}*\n"
                "💰 Сумма: {} ₽\n"
                "🏢 Партнёр: {}\n"
                "📅 Дата: {}\n"
                "🔗 [Ссылка на чек]({})\n\n"
                "{}"
            ).format(
                debtor_fio, amount_fmt, partner,
                date or "не определена", public_url, sheet_status
            ),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        logger.info(
            "Done: %s, %s RUB, partner=%s, url=%s",
            debtor_fio, amount, partner, public_url,
        )

    except Exception as e:
        logger.exception("Error in finish_processing")
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=processing_msg_id,
            text="❌ Ошибка: `{}`\nПопробуйте ещё раз.".format(e),
            parse_mode="Markdown",
        )
    finally:
        # Cleanup temp files
        try:
            tmp_path.unlink(missing_ok=True)
            tmp_path.parent.rmdir()
        except Exception:
            pass
