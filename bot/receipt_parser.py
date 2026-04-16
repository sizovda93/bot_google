"""Extract debtor FIO, amount, and date from bank receipts using OpenAI-compatible Vision API."""

import base64
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pdfplumber
from openai import AsyncOpenAI

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
    debtor_fio: Optional[str]
    amount: Optional[float]
    date: Optional[str]


def _extract_text_from_pdf(file_path: Path) -> Optional[str]:
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


def _parse_response(response_text: str) -> ReceiptData:
    """Parse JSON from LLM response."""
    cleaned = response_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.error("Failed to parse response as JSON: %s", response_text)
        return ReceiptData(debtor_fio=None, amount=None, date=None)

    amount = data.get("amount")
    if isinstance(amount, str):
        amount = float(amount.replace(" ", "").replace(",", ".").replace("₽", ""))

    return ReceiptData(
        debtor_fio=data.get("debtor_fio"),
        amount=amount,
        date=data.get("date"),
    )


CAPTION_PROMPT = """\
Из подписи к чеку в Telegram-чате извлеки ФИО клиентов (должников) и название партнёра.

ВАЖНО: в одном чеке может быть оплата за НЕСКОЛЬКИХ клиентов сразу!

Примеры:
- "оплата Кухотов Региональный Юр центр 14" → клиенты: ["Кухотов"], партнёр: Региональный Юр центр 14
- "оплата Иванов И.А., Петров С.В. Давид Ростов" → клиенты: ["Иванов И.А.", "Петров С.В."], партнёр: Давид Ростов
- "Мартыненко С. Публикации на депозите АС." → клиенты: ["Мартыненко С."], партнёр: null
- "Сысолятин С. Публикации на депозите АС.\\nДмитрий КРД161" → клиенты: ["Сысолятин С."], партнёр: Дмитрий КРД161
- "оплата Иванов + Петров Давид Ростов" → клиенты: ["Иванов", "Петров"], партнёр: Давид Ростов
- "Чек об оплате публикации Максимов А.А. и Сидорова Н.И." → клиенты: ["Максимов А.А.", "Сидорова Н.И."], партнёр: null
- "депозит Сысолятин СМИ" → клиенты: ["Сысолятин"], партнёр: СМИ

Правила:
- Клиент (должник) — это ФАМИЛИЯ человека (иногда с инициалами/именем-отчеством). Обычно идёт после слов "оплата", "публикации", "депозит", "чек об оплате".
- Несколько клиентов разделяются запятой, "и", "+", или перечислением.
- Партнёр — это НАЗВАНИЕ организации/компании/города. Обычно идёт ПОСЛЕ фамилий клиентов или на отдельной строке.
- Слова "оплата", "депозит", "публикации", "чек об оплате" — это НЕ часть ФИО и НЕ партнёр.

Также определи, есть ли в подписи слово "депозит" / "на депозите" / "депозита" — это значит оплата с депозита.

Верни ТОЛЬКО валидный JSON без markdown-обёртки:
{"clients": ["Фамилия И.О.", "Фамилия2 И.О."], "partner": "Название партнёра", "is_deposit": true}

Если партнёра не видно — поставь null. is_deposit = true если есть слово "депозит", иначе false.
Список клиентов — всегда массив (даже если один).
"""


@dataclass
class CaptionData:
    clients: list  # List[str]
    partner: Optional[str]
    is_deposit: bool


async def parse_caption(
    caption: str, api_key: str, base_url: str
) -> CaptionData:
    """Extract client FIOs and partner name from message caption using LLM."""
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    response = await client.chat.completions.create(
        model="gpt-5.4-mini",
        max_tokens=300,
        messages=[
            {
                "role": "user",
                "content": f"{CAPTION_PROMPT}\n\nПодпись: {caption}",
            }
        ],
    )

    text = response.choices[0].message.content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        data = json.loads(text)
        clients = data.get("clients") or []
        partner = data.get("partner")
        is_deposit = bool(data.get("is_deposit", False))
        # Backward compat: if old format with client_fio
        if not clients and data.get("client_fio"):
            clients = [data["client_fio"]]
        logger.info("Caption parsed: clients=%s, partner=%s, deposit=%s", clients, partner, is_deposit)
        return CaptionData(clients=clients, partner=partner, is_deposit=is_deposit)
    except json.JSONDecodeError:
        logger.warning("Failed to parse caption response: %s", text)
        return CaptionData(clients=[], partner=None, is_deposit=False)


async def parse_receipt(
    file_path: Path, api_key: str, base_url: str
) -> ReceiptData:
    """Parse a receipt file (PDF or image) and extract structured data."""
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    suffix = file_path.suffix.lower()
    model = "gpt-5.4-mini"

    # Try text extraction for PDFs first (cheaper, faster)
    if suffix == ".pdf":
        text = _extract_text_from_pdf(file_path)
        if text:
            logger.info("PDF has extractable text (%d chars), using text mode", len(text))
            response = await client.chat.completions.create(
                model=model,
                max_tokens=500,
                messages=[
                    {
                        "role": "user",
                        "content": f"{EXTRACTION_PROMPT}\n\nТекст чека:\n{text}",
                    }
                ],
            )
            return _parse_response(response.choices[0].message.content)

    # Vision mode: for images and image-only PDFs
    file_bytes = file_path.read_bytes()
    b64 = base64.standard_b64encode(file_bytes).decode("utf-8")

    if suffix in (".jpg", ".jpeg"):
        media_type = "image/jpeg"
    elif suffix == ".png":
        media_type = "image/png"
    elif suffix == ".webp":
        media_type = "image/webp"
    else:
        media_type = "image/jpeg"

    logger.info("Using vision mode for %s (%s)", file_path.name, media_type)

    response = await client.chat.completions.create(
        model=model,
        max_tokens=500,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media_type};base64,{b64}",
                        },
                    },
                    {"type": "text", "text": EXTRACTION_PROMPT},
                ],
            }
        ],
    )

    return _parse_response(response.choices[0].message.content)
