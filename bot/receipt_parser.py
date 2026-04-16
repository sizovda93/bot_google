"""Extract debtor FIO, amount, and date from bank receipts using Claude Vision."""

import base64
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import anthropic
import pdfplumber

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """\
Ты анализируешь банковский чек или квитанцию об оплате.

Извлеки из документа:
1. **ФИО должника** — это тот, ЗА КОГО платят (не плательщик!). \
Ищи в полях «Назначение платежа», «Сообщение», «Назначение перевода». \
Обычно после слов «публикации», «на публикации», «по делу о банкротстве» \
идёт фамилия должника. Верни полное ФИО если оно есть, или сокращённое (Фамилия И.О.) если полного нет.
2. **Сумму платежа** — основная сумма БЕЗ комиссии. Ищи поле «Сумма», «Сумма платежа», «Сумма операции». \
НЕ бери «Итого» если рядом есть отдельная «Комиссия».
3. **Дату операции** — в формате ДД.ММ.ГГГГ.

Верни ТОЛЬКО валидный JSON без markdown-обёртки:
{"debtor_fio": "Фамилия Имя Отчество", "amount": 21000, "date": "08.04.2026"}

Если какое-то поле невозможно определить — поставь null.
Сумму верни как число (int или float), без пробелов и символа рубля.
"""


@dataclass
class ReceiptData:
    debtor_fio: str | None
    amount: float | None
    date: str | None


def _extract_text_from_pdf(file_path: Path) -> str | None:
    """Try to extract text from PDF. Returns None if PDF is image-only."""
    try:
        with pdfplumber.open(file_path) as pdf:
            texts = []
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    texts.append(text)
            full_text = "\n".join(texts).strip()
            return full_text if len(full_text) > 50 else None
    except Exception as e:
        logger.warning("Failed to extract text from PDF: %s", e)
        return None


def _parse_claude_response(response_text: str) -> ReceiptData:
    """Parse JSON from Claude's response."""
    cleaned = response_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.error("Failed to parse Claude response as JSON: %s", response_text)
        return ReceiptData(debtor_fio=None, amount=None, date=None)

    amount = data.get("amount")
    if isinstance(amount, str):
        amount = float(amount.replace(" ", "").replace(",", ".").replace("₽", ""))

    return ReceiptData(
        debtor_fio=data.get("debtor_fio"),
        amount=amount,
        date=data.get("date"),
    )


async def parse_receipt(file_path: Path, api_key: str) -> ReceiptData:
    """Parse a receipt file (PDF or image) and extract structured data."""
    client = anthropic.AsyncAnthropic(api_key=api_key)
    suffix = file_path.suffix.lower()

    # Try text extraction for PDFs first (cheaper, faster)
    if suffix == ".pdf":
        text = _extract_text_from_pdf(file_path)
        if text:
            logger.info("PDF has extractable text (%d chars), using text mode", len(text))
            response = await client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=500,
                messages=[
                    {
                        "role": "user",
                        "content": f"{EXTRACTION_PROMPT}\n\nТекст чека:\n{text}",
                    }
                ],
            )
            return _parse_claude_response(response.content[0].text)

    # Vision mode: for images and image-only PDFs
    file_bytes = file_path.read_bytes()
    b64 = base64.standard_b64encode(file_bytes).decode("utf-8")

    if suffix == ".pdf":
        media_type = "application/pdf"
    elif suffix in (".jpg", ".jpeg"):
        media_type = "image/jpeg"
    elif suffix == ".png":
        media_type = "image/png"
    elif suffix == ".webp":
        media_type = "image/webp"
    else:
        media_type = "application/octet-stream"

    logger.info("Using vision mode for %s (%s)", file_path.name, media_type)

    response = await client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document" if suffix == ".pdf" else "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": EXTRACTION_PROMPT},
                ],
            }
        ],
    )

    return _parse_claude_response(response.content[0].text)
