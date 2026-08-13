from __future__ import annotations

import csv
import io
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, UploadFile

from src.chat_ui.config import ROOT_DIR


def normalize_key(value: str) -> str:
	import re

	return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def safe_int(value: object, default: int = 0) -> int:
	try:
		return int(float(str(value)))
	except Exception:
		return default


def split_ticket_ids(value: str) -> list[str]:
	if not value:
		return []
	return [chunk.strip() for chunk in str(value).split(";") if chunk.strip()]


def read_csv_rows_from_path(path: Path) -> list[dict[str, str]]:
	if not path.exists():
		return []
	with path.open("r", encoding="utf-8-sig", newline="") as handle:
		reader = csv.DictReader(handle)
		return [{str(k): str(v or "").strip() for k, v in row.items()} for row in reader]


async def read_csv_rows_from_upload(upload: UploadFile | None) -> list[dict[str, str]]:
	if upload is None:
		return []
	filename = upload.filename or "uploaded.csv"
	if Path(filename).suffix.lower() not in {".csv", ".txt"}:
		raise HTTPException(status_code=400, detail=f"{filename} must be a CSV file (.csv).")
	raw = await upload.read()
	if not raw:
		return []

	try:
		text = raw.decode("utf-8-sig")
	except UnicodeDecodeError:
		text = raw.decode("latin-1", errors="ignore")

	text = text.replace("\r\n", "\n").replace("\r", "\n")
	if not text.strip():
		return []

	header_line = text.split("\n", 1)[0]
	delimiter_candidates = [",", ";", "\t", "|"]
	delimiter = max(delimiter_candidates, key=lambda candidate: header_line.count(candidate))

	try:
		reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
		return [{str(k): str(v or "").strip() for k, v in row.items()} for row in reader]
	except csv.Error as exc:
		raise HTTPException(
			status_code=400,
			detail=(
				f"Unable to parse {filename} as CSV. "
				"Please save it as a standard CSV with quoted values when fields contain new lines."
			),
		) from exc


def get_value(row: dict[str, str], candidates: tuple[str, ...]) -> str:
	normalized = {normalize_key(key): value for key, value in row.items()}
	for candidate in candidates:
		value = normalized.get(normalize_key(candidate), "")
		if value:
			return value
	return ""


def write_dict_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
	with path.open("w", encoding="utf-8-sig", newline="") as handle:
		writer = csv.DictWriter(handle, fieldnames=fieldnames)
		writer.writeheader()
		writer.writerows(rows)


def parse_changed_age_days(changed_date: str) -> int | None:
	if not changed_date:
		return None
	text = changed_date.strip()
	if not text:
		return None
	text = text.replace("Z", "+00:00")
	try:
		dt = datetime.fromisoformat(text)
		if dt.tzinfo is None:
			dt = dt.replace(tzinfo=timezone.utc)
		age_days = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).days
		return max(0, age_days)
	except Exception:
		return None


def run_python_command(command: list[str], stage: str) -> str:
	try:
		proc = subprocess.run(command, capture_output=True, text=True, cwd=str(ROOT_DIR), check=False)
	except Exception as exc:
		raise HTTPException(status_code=500, detail=f"{stage} failed to start: {exc}") from exc

	if proc.returncode != 0:
		stderr = (proc.stderr or "").strip()
		stdout = (proc.stdout or "").strip()
		combined = "\n".join(part for part in (stderr, stdout) if part)
		if not combined:
			combined = f"exit code {proc.returncode}"
		message = combined[-2000:]
		raise HTTPException(status_code=500, detail=f"{stage} failed (exit {proc.returncode}): {message}")

	return (proc.stdout or "").strip()
