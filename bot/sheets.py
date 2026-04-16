"""Google Sheets integration: search for debtor, update payment data."""

import logging
import re

import gspread
from google.oauth2.service_account import Credentials
from thefuzz import fuzz

logger = logging.getLogger(__name__)

# Column indices (0-based) matching the spreadsheet structure
COL_FIO = 1          # B — ФИО
COL_PARTNER = 3      # D — Партнер
COL_PLAN = 5         # F — Обязательные план
COL_FACT = 6         # G — Обязательные факт
COL_DEBT = 7         # H — Долг
COL_CHECK_LINK = 8   # I — Чек об оплате (ссылка)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

FUZZY_MATCH_THRESHOLD = 75


def _normalize_fio(fio: str) -> str:
    """Normalize FIO for comparison: lowercase, collapse whitespace."""
    fio = fio.lower().strip()
    fio = re.sub(r"\s+", " ", fio)
    # Remove dots from initials for better matching
    fio = fio.replace(".", " ").strip()
    fio = re.sub(r"\s+", " ", fio)
    return fio


def _parse_money(value: str) -> float:
    """Parse money string like 'р.21 000' or '21000' to float."""
    if not value:
        return 0.0
    cleaned = re.sub(r"[^\d.,]", "", str(value))
    cleaned = cleaned.replace(",", ".").replace(" ", "")
    try:
        return float(cleaned) if cleaned else 0.0
    except ValueError:
        return 0.0


def _format_money(amount: float) -> str:
    """Format amount as 'р.21 000' to match existing table format."""
    int_amount = int(amount)
    formatted = f"{int_amount:,}".replace(",", " ")
    return f"р.{formatted}"


class SheetsClient:
    def __init__(self, service_account_path: str, spreadsheet_id: str, gid: int):
        creds = Credentials.from_service_account_file(
            service_account_path, scopes=SCOPES
        )
        self.gc = gspread.authorize(creds)
        self.spreadsheet = self.gc.open_by_key(spreadsheet_id)
        self.worksheet = self._get_worksheet_by_gid(gid)
        logger.info(
            "Connected to sheet '%s', worksheet '%s'",
            self.spreadsheet.title,
            self.worksheet.title,
        )

    def _get_worksheet_by_gid(self, gid: int) -> gspread.Worksheet:
        """Find worksheet by GID."""
        for ws in self.spreadsheet.worksheets():
            if ws.id == gid:
                return ws
        raise ValueError(f"Worksheet with GID {gid} not found")

    def find_debtor_row(self, debtor_fio: str) -> tuple[int, dict] | None:
        """
        Search for debtor by FIO (fuzzy match).
        Returns (row_number_1based, row_data_dict) or None.
        """
        all_values = self.worksheet.get_all_values()
        if len(all_values) < 2:
            return None

        normalized_query = _normalize_fio(debtor_fio)
        best_match = None
        best_score = 0

        # Skip header row (index 0)
        for idx, row in enumerate(all_values[1:], start=2):
            if len(row) <= COL_FIO or not row[COL_FIO].strip():
                continue

            cell_fio = row[COL_FIO].strip()
            normalized_cell = _normalize_fio(cell_fio)

            # Try multiple fuzzy strategies
            score_ratio = fuzz.ratio(normalized_query, normalized_cell)
            score_partial = fuzz.partial_ratio(normalized_query, normalized_cell)
            score_sort = fuzz.token_sort_ratio(normalized_query, normalized_cell)
            score = max(score_ratio, score_partial, score_sort)

            if score > best_score:
                best_score = score
                best_match = (idx, row, cell_fio)

        if best_match and best_score >= FUZZY_MATCH_THRESHOLD:
            row_num, row_data, matched_fio = best_match
            logger.info(
                "Fuzzy match: '%s' → '%s' (score=%d, row=%d)",
                debtor_fio,
                matched_fio,
                best_score,
                row_num,
            )
            return row_num, {
                "fio": matched_fio,
                "partner": row_data[COL_PARTNER] if len(row_data) > COL_PARTNER else "",
                "plan": row_data[COL_PLAN] if len(row_data) > COL_PLAN else "",
                "fact": row_data[COL_FACT] if len(row_data) > COL_FACT else "",
                "debt": row_data[COL_DEBT] if len(row_data) > COL_DEBT else "",
                "check_link": row_data[COL_CHECK_LINK] if len(row_data) > COL_CHECK_LINK else "",
            }

        logger.warning(
            "No match for '%s' (best score=%d)", debtor_fio, best_score
        )
        return None

    def update_payment(self, row_num: int, amount: float, check_link: str) -> None:
        """Update the fact amount, debt, and check link for a row."""
        # Read current plan value to calculate new debt
        plan_cell = self.worksheet.cell(row_num, COL_PLAN + 1).value
        plan_amount = _parse_money(plan_cell)

        # Read current fact value (might have partial payment already)
        fact_cell = self.worksheet.cell(row_num, COL_FACT + 1).value
        current_fact = _parse_money(fact_cell)
        new_fact = current_fact + amount

        new_debt = max(0, plan_amount - new_fact)

        # Batch update: fact, debt, check link
        self.worksheet.update_cell(row_num, COL_FACT + 1, _format_money(new_fact))
        self.worksheet.update_cell(row_num, COL_DEBT + 1, _format_money(new_debt))

        # Append link (don't overwrite if there's already one)
        existing_link = self.worksheet.cell(row_num, COL_CHECK_LINK + 1).value
        if existing_link and existing_link.strip():
            new_link_value = f"{existing_link}\n{check_link}"
        else:
            new_link_value = check_link
        self.worksheet.update_cell(row_num, COL_CHECK_LINK + 1, new_link_value)

        logger.info(
            "Updated row %d: fact=%s, debt=%s, link=%s",
            row_num,
            _format_money(new_fact),
            _format_money(new_debt),
            check_link,
        )
