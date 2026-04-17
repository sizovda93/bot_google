"""Google Sheets integration: search for debtor, update payment data."""

import logging
import re
import time
from typing import Optional, Tuple

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
COL_COMMENT = 11     # L — Комментарий по оплате долга

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

FUZZY_MATCH_THRESHOLD = 75

RUSSIAN_VOWELS = set("аеёиоуыэюяaeiouy")


def _normalize_fio(fio: str) -> str:
    """Normalize FIO for comparison: lowercase, collapse whitespace."""
    fio = fio.lower().strip()
    fio = re.sub(r"\s+", " ", fio)
    # Remove dots from initials for better matching
    fio = fio.replace(".", " ").strip()
    fio = re.sub(r"\s+", " ", fio)
    return fio


def _consonant_skeleton(text: str) -> str:
    """Extract consonant skeleton from text (lowercase, only consonants).
    'Лозинина' → 'лзнн', 'Малинина' → 'млнн'
    Used to filter out false fuzzy matches where consonants differ.
    """
    text = text.lower()
    return "".join(c for c in text if c.isalpha() and c not in RUSSIAN_VOWELS)


def _consonants_compatible(query: str, candidate: str) -> bool:
    """Check if two FIOs have compatible consonant structure.
    Rules:
    - First letter of surname MUST match (case-insensitive)
    - Consonant skeletons must be identical, or query skeleton
      must be a prefix of candidate skeleton (for abbreviated names)
    """
    q_words = query.lower().split()
    c_words = candidate.lower().split()

    if not q_words or not c_words:
        return False

    # First letter of surname must match
    if q_words[0][0] != c_words[0][0]:
        return False

    q_skel = _consonant_skeleton(q_words[0])
    c_skel = _consonant_skeleton(c_words[0])

    # Exact match or prefix (for short/abbreviated surnames)
    if q_skel == c_skel:
        return True
    if c_skel.startswith(q_skel) and len(q_skel) >= 2:
        return True
    if q_skel.startswith(c_skel) and len(c_skel) >= 2:
        return True

    return False


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
    # Worksheet names to scan (in priority order — newest first)
    SHEET_NAMES = ["2026", "2025", "2024"]

    def __init__(self, service_account_path: str, spreadsheet_id: str, gid: int):
        creds = Credentials.from_service_account_file(
            service_account_path, scopes=SCOPES
        )
        self.gc = gspread.authorize(creds)
        self.spreadsheet = self.gc.open_by_key(spreadsheet_id)
        self.default_gid = gid

        # Load all target worksheets
        self.worksheets = {}  # type: dict  # name → gspread.Worksheet
        for ws in self.spreadsheet.worksheets():
            if ws.title in self.SHEET_NAMES:
                self.worksheets[ws.title] = ws
                logger.info("Loaded worksheet: '%s' (gid=%d)", ws.title, ws.id)

        if not self.worksheets:
            raise ValueError("No worksheets found with names: {}".format(self.SHEET_NAMES))

        # Cache: worksheet_name → (timestamp, all_values)
        self._cache = {}  # type: dict
        self._cache_ttl = 300  # 5 minutes

        logger.info(
            "Connected to sheet '%s', worksheets: %s",
            self.spreadsheet.title,
            list(self.worksheets.keys()),
        )

    def _get_cached_values(self, ws: gspread.Worksheet) -> list:
        """Get all values from worksheet, cached for 5 minutes."""
        now = time.time()
        cached = self._cache.get(ws.title)
        if cached and (now - cached[0]) < self._cache_ttl:
            return cached[1]

        values = ws.get_all_values()
        self._cache[ws.title] = (now, values)
        logger.info("Cached %d rows from '%s'", len(values), ws.title)
        return values

    def _invalidate_cache(self, ws_title: str) -> None:
        """Clear cache for a worksheet after writing."""
        self._cache.pop(ws_title, None)

    def _search_in_worksheet(
        self, ws: gspread.Worksheet, debtor_fio: str, partner_hint: Optional[str]
    ) -> list:
        """Search for debtor in a single worksheet. Returns list of candidates."""
        all_values = self._get_cached_values(ws)
        if len(all_values) < 2:
            return []

        normalized_query = _normalize_fio(debtor_fio)
        candidates = []

        for idx, row in enumerate(all_values[1:], start=2):
            if len(row) <= COL_FIO or not row[COL_FIO].strip():
                continue

            cell_fio = row[COL_FIO].strip()
            normalized_cell = _normalize_fio(cell_fio)

            if not _consonants_compatible(normalized_query, normalized_cell):
                continue

            score_ratio = fuzz.ratio(normalized_query, normalized_cell)
            score_partial = fuzz.partial_ratio(normalized_query, normalized_cell)
            score_sort = fuzz.token_sort_ratio(normalized_query, normalized_cell)
            fio_score = max(score_ratio, score_partial, score_sort)

            if fio_score >= FUZZY_MATCH_THRESHOLD:
                cell_partner = row[COL_PARTNER].strip() if len(row) > COL_PARTNER else ""
                candidates.append((idx, row, cell_fio, cell_partner, fio_score, ws))

        return candidates

    def find_debtor_row(
        self, debtor_fio: str, partner_hint: Optional[str] = None
    ) -> Optional[Tuple[int, dict]]:
        """
        Search for debtor across all worksheets (2026, 2025, 2024).
        If partner_hint is given, uses it to disambiguate duplicates.
        Returns (row_number_1based, row_data_dict) or None.
        row_data_dict includes '_worksheet' key for update_payment.
        """
        all_candidates = []

        for sheet_name in self.SHEET_NAMES:
            ws = self.worksheets.get(sheet_name)
            if not ws:
                continue
            candidates = self._search_in_worksheet(ws, debtor_fio, partner_hint)
            all_candidates.extend(candidates)

        if not all_candidates:
            logger.warning("No match for '%s' in any worksheet", debtor_fio)
            return None

        # If partner hint given — pick the candidate whose partner matches best
        if partner_hint and len(all_candidates) > 1:
            norm_hint = _normalize_fio(partner_hint)
            best = None
            best_combined = 0
            for idx, row, cell_fio, cell_partner, fio_score, ws in all_candidates:
                partner_score = fuzz.partial_ratio(norm_hint, _normalize_fio(cell_partner))
                combined = fio_score + partner_score
                if combined > best_combined:
                    best_combined = combined
                    best = (idx, row, cell_fio, cell_partner, fio_score, ws)
            if best:
                all_candidates = [best]

        # Take best by FIO score
        best_candidate = max(all_candidates, key=lambda c: c[4])
        row_num, row_data, matched_fio, matched_partner, score, ws = best_candidate

        logger.info(
            "Fuzzy match: '%s' → '%s' (score=%d, partner='%s', row=%d, sheet='%s')",
            debtor_fio, matched_fio, score, matched_partner, row_num, ws.title,
        )
        return row_num, {
            "fio": matched_fio,
            "partner": row_data[COL_PARTNER] if len(row_data) > COL_PARTNER else "",
            "plan": row_data[COL_PLAN] if len(row_data) > COL_PLAN else "",
            "fact": row_data[COL_FACT] if len(row_data) > COL_FACT else "",
            "debt": row_data[COL_DEBT] if len(row_data) > COL_DEBT else "",
            "check_link": row_data[COL_CHECK_LINK] if len(row_data) > COL_CHECK_LINK else "",
            "_worksheet": ws,
            "_sheet_name": ws.title,
        }

    def update_payment(
        self, row_num: int, amount: float, check_link: str,
        comment: Optional[str] = None, worksheet: "Optional[gspread.Worksheet]" = None
    ) -> None:
        """Update the fact amount, debt, check link, and optionally comment."""
        # Use specific worksheet if provided, otherwise first available
        ws = worksheet or list(self.worksheets.values())[0]

        fact_cell = ws.cell(row_num, COL_FACT + 1).value
        current_fact = _parse_money(fact_cell)
        new_fact = current_fact + amount

        ws.update_cell(row_num, COL_FACT + 1, _format_money(new_fact))
        # COL_DEBT (H) не трогаем — там формула, пересчитается автоматически

        existing_link = ws.cell(row_num, COL_CHECK_LINK + 1).value
        if existing_link and existing_link.strip():
            new_link_value = "{}\n{}".format(existing_link, check_link)
        else:
            new_link_value = check_link
        ws.update_cell(row_num, COL_CHECK_LINK + 1, new_link_value)

        if comment:
            ws.update_cell(row_num, COL_COMMENT + 1, comment)

        # Invalidate cache after writing
        self._invalidate_cache(ws.title)

        logger.info(
            "Updated row %d in '%s': fact=%s, link=%s",
            row_num, ws.title,
            _format_money(new_fact),
            check_link,
        )
