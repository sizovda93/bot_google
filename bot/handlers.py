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
    await _finish_multi(
        message=message,
        bot=bot,
        clients=data["clients"],
        amount_per_client=data["amount_per_client"],
        total_amount=data["total_amount"],
        date=data["date"],
        caption_partner=partner,
        is_deposit=data.get("is_deposit", False),
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

        # Step 2: Extract client FIOs + partner from caption (via LLM)
        caption_data = CaptionData(clients=[], partner=None, is_deposit=False)
        if message.caption:
            caption_data = await parse_caption(
                message.caption, config.openai_api_key, config.openai_base_url
            )
            logger.info("Caption: clients=%s, partner=%s", caption_data.clients, caption_data.partner)

        # Step 3: Parse receipt (amount, date, and backup FIO)
        receipt = await parse_receipt(
            tmp_path, config.openai_api_key, config.openai_base_url
        )
        logger.info("Parsed receipt: %s", receipt)

        # Build client list: caption clients first, receipt FIO as fallback
        clients = caption_data.clients
        if not clients and receipt.debtor_fio:
            clients = [receipt.debtor_fio]
        caption_partner = caption_data.partner

        if not clients:
            await processing_msg.edit_text(
                "❌ Не удалось определить ФИО клиента.\n"
                "Не нашёл ни в подписи к сообщению, ни в самом чеке.\n"
                "Проверьте качество файла или добавьте данные вручную."
            )
            return

        if not receipt.amount:
            await processing_msg.edit_text(
                "❌ Распознал клиентов: *{}*, "
                "но не смог определить сумму.\n"
                "Проверьте чек вручную.".format(", ".join(clients)),
                parse_mode="Markdown",
            )
            return

        # Step 4: Multi-client processing
        num_clients = len(clients)
        amount_per_client = receipt.amount / num_clients

        await _finish_multi(
            message=message,
            bot=bot,
            clients=clients,
            amount_per_client=amount_per_client,
            total_amount=receipt.amount,
            date=receipt.date,
            caption_partner=caption_partner,
            is_deposit=caption_data.is_deposit,
            tmp_path=tmp_path,
            ext=ext,
            processing_msg_id=processing_msg.message_id,
        )

    except Exception as e:
        logger.exception("Error processing receipt")
        await processing_msg.edit_text(
            "❌ Ошибка при обработке чека:\n`{}`\n\n"
            "Попробуйте ещё раз или обработайте вручную.".format(e),
            parse_mode="Markdown",
        )


async def _finish_multi(
    message: Message,
    bot: Bot,
    clients: list,
    amount_per_client: float,
    total_amount: float,
    date: Optional[str],
    caption_partner: Optional[str],
    is_deposit: bool,
    tmp_path: Path,
    ext: str,
    processing_msg_id: int,
) -> None:
    """Upload one receipt, update sheet for each client, confirm in chat."""
    try:
        # Step 5: Find first client to determine partner for YaDisk folder
        first_match = None
        partner = caption_partner
        for c in clients:
            m = sheets.find_debtor_row(c, partner_hint=caption_partner)
            if m:
                first_match = m
                partner = m[1]["partner"]
                break

        if not partner:
            # No partner from table or caption — ask via reply
            clients_str = ", ".join(clients)
            amount_fmt = "{:,.0f}".format(total_amount).replace(",", " ")
            ask_msg = await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=processing_msg_id,
                text=(
                    "⚠️ Не смог определить партнёра.\n"
                    "Клиенты: *{}*\n"
                    "Сумма: {} ₽\n\n"
                    "Ответьте на это сообщение — укажите *имя партнёра*."
                ).format(clients_str, amount_fmt),
                parse_mode="Markdown",
            )
            pending_receipts[ask_msg.message_id] = {
                "clients": clients,
                "amount_per_client": amount_per_client,
                "total_amount": total_amount,
                "date": date,
                "is_deposit": is_deposit,
                "tmp_path": str(tmp_path),
                "ext": ext,
                "processing_msg_id": ask_msg.message_id,
            }
            return

        # Step 6: Upload to Yandex Disk ONCE (one receipt = one file)
        filename_parts = [_make_target_filename(c, "").rstrip(".") for c in clients]
        target_filename = ", ".join(filename_parts) + ext
        # If filename too long, truncate
        if len(target_filename) > 200:
            target_filename = filename_parts[0] + " и др" + ext

        public_url = yadisk_client.upload_and_share(
            local_path=tmp_path,
            partner_name=partner,
            target_filename=target_filename,
        )

        # Step 7: Update Google Sheet for EACH client
        results = []
        for client_fio in clients:
            match = sheets.find_debtor_row(client_fio, partner_hint=caption_partner)
            if match:
                row_num, row_data = match
                # Build comment for deposit payments
                deposit_comment = None
                if is_deposit:
                    amt_fmt = "{:,.0f}".format(amount_per_client).replace(",", " ")
                    deposit_comment = "{} с депозита".format(amt_fmt)

                sheets.update_payment(
                    row_num=row_num,
                    amount=amount_per_client,
                    check_link=public_url,
                    comment=deposit_comment,
                )
                results.append("✅ {} — строка {} ({})".format(
                    row_data["fio"], row_num, row_data["partner"]
                ))
                logger.info("Updated: %s, row %d, %s RUB", row_data["fio"], row_num, amount_per_client)
            else:
                results.append("⚠️ {} — не найден в таблице".format(client_fio))
                logger.warning("Not found in sheet: %s", client_fio)

        # Step 8: Confirm in chat
        amount_fmt = "{:,.0f}".format(amount_per_client).replace(",", " ")
        total_fmt = "{:,.0f}".format(total_amount).replace(",", " ")

        if len(clients) > 1:
            amount_line = "💰 Сумма: {} ₽ ({} ₽ на {} чел.)".format(
                total_fmt, amount_fmt, len(clients)
            )
        else:
            amount_line = "💰 Сумма: {} ₽".format(amount_fmt)

        client_lines = "\n".join(results)
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=processing_msg_id,
            text=(
                "✅ Чек обработан!\n\n"
                "{}\n"
                "🏢 Партнёр: {}\n"
                "📅 Дата: {}\n"
                "🔗 [Ссылка на чек]({})\n\n"
                "{}"
            ).format(
                amount_line, partner,
                date or "не определена", public_url, client_lines
            ),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )

    except Exception as e:
        logger.exception("Error in finish_multi")
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=processing_msg_id,
            text="❌ Ошибка: `{}`\nПопробуйте ещё раз.".format(e),
            parse_mode="Markdown",
        )
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
            tmp_path.parent.rmdir()
        except Exception:
            pass
