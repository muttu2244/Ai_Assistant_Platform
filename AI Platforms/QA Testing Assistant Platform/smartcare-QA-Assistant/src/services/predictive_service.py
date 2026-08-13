from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

from fastapi import HTTPException, UploadFile

from src.chat_ui.config import (
	DEFAULT_FEATURE_MODULE_CSV,
	DEFAULT_FUNCTIONALITY_SUMMARY_CSV,
	DEFAULT_MODULE_SUMMARY_CSV,
	DEFAULT_MSP_SHEET_NAME,
	DEFAULT_MSP_TARGET_CATEGORY,
	DEFAULT_RECENT_WINDOW_DAYS,
	DEFAULT_RECURRENCE_OUTPUT_CSV,
	DEFAULT_TICKET_EXPORT_CSV,
	ROOT_DIR,
)
from src.repositories.predictive_repository import (
	get_value,
	parse_changed_age_days,
	read_csv_rows_from_path,
	read_csv_rows_from_upload,
	run_python_command,
	safe_int,
	split_ticket_ids,
	normalize_key,
	write_dict_csv,
)


PREDICTIVE_RUN_STATUS: dict[str, dict[str, object]] = {}


def set_predictive_run_status(run_id: str, state: str, step: str, message: str) -> None:
	PREDICTIVE_RUN_STATUS[run_id] = {
		"run_id": run_id,
		"state": state,
		"step": step,
		"message": message,
		"updated_at": datetime.now(timezone.utc).isoformat(),
	}


def derive_modified_functions_from_feature_rows(feature_rows: list[dict[str, str]]) -> list[dict[str, str]]:
	seen: set[str] = set()
	derived: list[dict[str, str]] = []
	for row in feature_rows:
		functionality = get_value(row, ("functionality", "feature_functionality")).strip()
		if not functionality:
			continue
		key = normalize_key(functionality)
		if not key or key in seen:
			continue
		seen.add(key)
		derived.append({"modified_functionality": functionality})
	return derived


def compute_predictive_outputs(
	module_summary_rows: list[dict[str, str]],
	functionality_summary_rows: list[dict[str, str]],
	feature_module_rows: list[dict[str, str]],
	dependency_rows: list[dict[str, str]],
	modified_function_rows: list[dict[str, str]],
	ticket_rows: list[dict[str, str]],
	output_csv_path: Path,
	recent_window_days: int = DEFAULT_RECENT_WINDOW_DAYS,
	top_score_levels: int = 50,
) -> dict[str, object]:
	module_feature_counts: dict[str, int] = {}
	for row in module_summary_rows:
		module_name = get_value(row, ("module_name", "module"))
		if not module_name:
			continue
		module_feature_counts[module_name] = safe_int(get_value(row, ("feature_count",)))

	functionality_to_ids: dict[str, list[str]] = {}
	functionality_to_module: dict[str, str] = {}
	functionality_to_count: dict[str, int] = {}
	module_to_unique_ticket_ids: dict[str, set[str]] = {}
	for row in functionality_summary_rows:
		func = get_value(row, ("functionality", "feature_functionality"))
		module_name = get_value(row, ("module_name", "module"))
		ids = split_ticket_ids(get_value(row, ("bug_cust_ticket_ids", "bug_ticket_ids")))
		count = safe_int(get_value(row, ("bug_cust_ticket_count", "bug_ticket_count", "count")), default=len(ids))
		if not func:
			continue
		functionality_to_ids[func] = ids
		functionality_to_count[func] = count
		functionality_to_module[func] = module_name
		if module_name:
			module_to_unique_ticket_ids.setdefault(module_name, set()).update(ids)

	module_cards = []
	for module_name in sorted(set(module_feature_counts) | set(module_to_unique_ticket_ids)):
		module_cards.append({
			"module_name": module_name,
			"ticket_count": len(module_to_unique_ticket_ids.get(module_name, set())),
			"feature_count": module_feature_counts.get(module_name, 0),
		})
	module_cards.sort(key=lambda item: item["ticket_count"], reverse=True)

	functionality_to_features: dict[str, set[str]] = {}
	for row in feature_module_rows:
		func = get_value(row, ("functionality", "feature_functionality"))
		feature_name = get_value(row, ("title", "feature_name"))
		if not func:
			continue
		functionality_to_features.setdefault(func, set())
		if feature_name:
			functionality_to_features[func].add(feature_name)

	functionality_lookup: dict[str, str] = {
		normalize_key(func): func for func in functionality_to_ids if normalize_key(func)
	}

	def resolve_functionality_name(name: str) -> str:
		key = normalize_key(name)
		if not key:
			return ""
		return functionality_lookup.get(key, name.strip())

	dependency_map: dict[str, set[str]] = {}
	for row in dependency_rows:
		source = get_value(row, ("source_functionality", "functionality", "function", "from_function", "from", "function_code", "source_function"))
		target = get_value(row, ("depends_on_functionality", "dependent_functionality", "depends_on", "to_function", "to", "depend_code", "dependent_code", "target_function"))
		source = resolve_functionality_name(source)
		target = resolve_functionality_name(target)
		if source and target:
			dependency_map.setdefault(source, set()).add(target)

	modified_functions: set[str] = set()
	for row in modified_function_rows:
		val = get_value(row, ("modified_functionality", "modified_function", "functionality", "function"))
		if val:
			modified_functions.add(resolve_functionality_name(val))

	if not modified_functions:
		ranked = sorted(functionality_to_count.items(), key=lambda item: item[1], reverse=True)
		modified_functions = {name for name, _ in ranked[:8]}

	ticket_meta: dict[str, dict[str, str]] = {}
	for row in ticket_rows:
		ticket_id = get_value(row, ("id", "ticket_id", "bug_ticket_id")).strip()
		if not ticket_id:
			continue
		ticket_meta[ticket_id] = {
			"customer_priority": get_value(row, ("customer_priority", "priority", "customer priority")),
			"changed_date": get_value(row, ("changed_date", "system.changeddate")),
			"work_item_type": get_value(row, ("work_item_type", "type", "system.workitemtype")),
			"title": get_value(row, ("title", "system.title")),
			"state": get_value(row, ("state", "system.state")),
			"area_path": get_value(row, ("area_path", "system.areapath")),
		}

	def priority_points(priority: str) -> tuple[int, str, int]:
		value = (priority or "").strip().lower()
		if value == "on fire":
			return 25, "PRIORITY_ON_FIRE", 3
		if value == "urgent":
			return 20, "PRIORITY_URGENT", 2
		if value == "high":
			return 15, "PRIORITY_HIGH", 1
		return 0, "PRIORITY_OTHER", 0

	def recency_points(changed_date: str) -> tuple[int, str, int]:
		age_days = parse_changed_age_days(changed_date)
		if age_days is None:
			return 0, "NO_RECENCY_DATA", 9999
		if age_days <= recent_window_days:
			return 15, f"RECENT_0_{recent_window_days}D", age_days
		if age_days <= recent_window_days * 2:
			return 8, f"RECENT_{recent_window_days + 1}_{recent_window_days * 2}D", age_days
		return 2, f"OLDER_{recent_window_days * 2}D_PLUS", age_days

	ticket_candidates: dict[str, dict[str, object]] = {}
	for modified_func in sorted(modified_functions):
		contexts: list[tuple[str, str, int]] = [(modified_func, "direct_change", 0)]
		for dependent in sorted(dependency_map.get(modified_func, set())):
			contexts.append((dependent, "dependent_change", 1))

		for candidate_func, relationship_type, dependency_distance in contexts:
			for ticket_id in functionality_to_ids.get(candidate_func, []):
				meta = ticket_meta.get(ticket_id, {})
				priority_score, priority_code, priority_rank = priority_points(str(meta.get("customer_priority", "")))
				recency_score, recency_code, age_days = recency_points(str(meta.get("changed_date", "")))
				match_points = 50 if dependency_distance == 0 else 35
				distance_penalty = dependency_distance * 7
				raw_score = max(0, min(100, match_points + priority_score + recency_score - distance_penalty))
				reason_codes = ["DIRECT_MATCH" if dependency_distance == 0 else f"DEP_{dependency_distance}_HOP", priority_code, recency_code]

				row = {
					"ticket_id": ticket_id,
					"module_name": functionality_to_module.get(candidate_func, functionality_to_module.get(modified_func, "")),
					"feature_name": "; ".join(sorted(functionality_to_features.get(candidate_func, set()))),
					"modified_functionality": modified_func,
					"dependent_functionality": candidate_func,
					"relationship_type": relationship_type,
					"dependency_distance": dependency_distance,
					"impact_score_raw": raw_score,
					"impact_reason": "; ".join(reason_codes),
					"customer_priority": str(meta.get("customer_priority", "")),
					"changed_date": str(meta.get("changed_date", "")),
					"work_item_type": str(meta.get("work_item_type", "")),
					"title": str(meta.get("title", "")),
					"state": str(meta.get("state", "")),
					"area_path": str(meta.get("area_path", "")),
					"priority_rank": priority_rank,
					"age_days": age_days,
				}

				existing = ticket_candidates.get(ticket_id)
				if existing is None:
					ticket_candidates[ticket_id] = row
					continue

				existing_score = int(existing.get("impact_score_raw", 0))
				if raw_score > existing_score or (raw_score == existing_score and dependency_distance < int(existing.get("dependency_distance", 99))):
					ticket_candidates[ticket_id] = row

	scored_rows = list(ticket_candidates.values())
	scored_rows.sort(
		key=lambda item: (
			-int(item.get("impact_score_raw", 0)),
			int(item.get("dependency_distance", 99)),
			-int(item.get("priority_rank", 0)),
			int(item.get("age_days", 9999)) if isinstance(item.get("age_days", 9999), int) else 9999,
			str(item.get("ticket_id", "")),
		)
	)

	last_raw: int | None = None
	continuous_score = 100
	for row in scored_rows:
		raw = int(row.get("impact_score_raw", 0))
		if last_raw is None:
			continuous_score = 100
		elif raw < last_raw:
			continuous_score = max(1, continuous_score - 1)
		row["impact_score"] = continuous_score
		last_raw = raw
		if continuous_score >= 85:
			row["risk_level"] = "High"
		elif continuous_score >= 70:
			row["risk_level"] = "Medium"
		elif continuous_score > 0:
			row["risk_level"] = "Low"
		else:
			row["risk_level"] = "None"

	detailed_rows = [{
		"ticket_id": str(row.get("ticket_id", "")),
		"impact_score": str(row.get("impact_score", "")),
		"impact_score_raw": str(row.get("impact_score_raw", "")),
		"dependency_distance": str(row.get("dependency_distance", "")),
		"impact_reason": str(row.get("impact_reason", "")),
		"risk_level": str(row.get("risk_level", "")),
		"relationship_type": str(row.get("relationship_type", "")),
		"module_name": str(row.get("module_name", "")),
		"feature_name": str(row.get("feature_name", "")),
		"modified_functionality": str(row.get("modified_functionality", "")),
		"dependent_functionality": str(row.get("dependent_functionality", "")),
		"customer_priority": str(row.get("customer_priority", "")),
		"changed_date": str(row.get("changed_date", "")),
		"work_item_type": str(row.get("work_item_type", "")),
		"state": str(row.get("state", "")),
		"title": str(row.get("title", "")),
		"area_path": str(row.get("area_path", "")),
	} for row in scored_rows]

	write_dict_csv(
		output_csv_path,
		[
			"ticket_id", "impact_score", "impact_score_raw", "dependency_distance", "impact_reason", "risk_level",
			"relationship_type", "module_name", "feature_name", "modified_functionality", "dependent_functionality",
			"customer_priority", "changed_date", "work_item_type", "state", "title", "area_path",
		],
		detailed_rows,
	)

	score_context_groups: dict[tuple[int, str, str, str], list[dict[str, object]]] = {}
	for row in scored_rows:
		score = int(row.get("impact_score", 0))
		group_key = (score, str(row.get("modified_functionality", "")), str(row.get("dependent_functionality", "")), str(row.get("relationship_type", "")))
		score_context_groups.setdefault(group_key, []).append(row)

	ordered_groups = sorted(score_context_groups.items(), key=lambda item: (-item[0][0], -len(item[1]), item[0][2].lower(), item[0][1].lower()))

	grouped_rows_all: list[dict[str, str]] = []
	for (score, modified_func, dependent_func, relationship_type), group in ordered_groups:
		ticket_ids = [str(item.get("ticket_id", "")) for item in group if str(item.get("ticket_id", ""))]
		modules = sorted({str(item.get("module_name", "")) for item in group if str(item.get("module_name", ""))})
		features = sorted({str(item.get("feature_name", "")) for item in group if str(item.get("feature_name", ""))})
		reasons = sorted({str(item.get("impact_reason", "")) for item in group if str(item.get("impact_reason", ""))})
		min_distance = min(int(item.get("dependency_distance", 99)) for item in group)
		risk_rank = {"High": 3, "Medium": 2, "Low": 1, "None": 0}
		risk_level = sorted((str(item.get("risk_level", "None")) for item in group), key=lambda value: risk_rank.get(value, 0), reverse=True)[0]
		preview = ticket_ids[:5]
		more = max(0, len(ticket_ids) - len(preview))
		preview_text = "; ".join(preview)
		grouped_rows_all.append({
			"impact_score": str(score),
			"ticket_count": str(len(ticket_ids)),
			"ticket_ids_preview": preview_text,
			"ticket_ids_all": "; ".join(ticket_ids),
			"ticket_more_count": str(more),
			"module_name": modules[0] if modules else "",
			"feature_name": "; ".join(features[:2]),
			"modified_functionality": modified_func,
			"dependent_functionality": dependent_func,
			"relationship_type": relationship_type,
			"dependency_distance": str(min_distance),
			"impact_reason": reasons[0] if reasons else "",
			"risk_level": risk_level,
		})

	max_client_score_levels = 500
	grouped_rows = grouped_rows_all[:top_score_levels]
	grouped_rows_client = grouped_rows_all[:max_client_score_levels]

	risk_counts = {
		"high": sum(1 for row in scored_rows if str(row.get("risk_level")) == "High"),
		"medium": sum(1 for row in scored_rows if str(row.get("risk_level")) == "Medium"),
		"low": sum(1 for row in scored_rows if str(row.get("risk_level")) == "Low"),
	}

	functionality_cards = []
	for func, count in sorted(functionality_to_count.items(), key=lambda item: item[1], reverse=True):
		functionality_cards.append({"functionality": func, "module_name": functionality_to_module.get(func, ""), "ticket_count": count})

	return {
		"cards": {
			"total_modules": len(module_cards),
			"total_functionalities": len(functionality_to_count),
			"total_modified_functions": len(modified_functions),
			"candidate_rows": len(scored_rows),
			"high_risk_rows": risk_counts["high"],
		},
		"top_modules": module_cards,
		"top_functionalities": functionality_cards,
		"risk_counts": risk_counts,
		"recurrence_candidates": grouped_rows,
		"recurrence_candidates_all": grouped_rows_client,
		"available_score_levels": len(grouped_rows_client),
		"total_score_levels": len(grouped_rows_all),
		"outputs": {
			"recurrence_csv": str(output_csv_path),
			"module_summary_csv": str(DEFAULT_MODULE_SUMMARY_CSV),
			"functionality_summary_csv": str(DEFAULT_FUNCTIONALITY_SUMMARY_CSV),
		},
	}


async def run_predictive_defaults_service(
	release_name: str,
	days: int,
	top_score_levels: int,
	work_item_scope: str,
	priority_scope: str,
	match_mode: str,
	include_dependencies: bool,
	modified_functions_csv: UploadFile | None,
	dependency_metrics_csv: UploadFile | None,
	feature_module_csv: UploadFile | None,
	module_summary_csv: UploadFile | None,
	functionality_summary_csv: UploadFile | None,
) -> dict[str, object]:
	module_rows = await read_csv_rows_from_upload(module_summary_csv)
	if not module_rows:
		module_rows = read_csv_rows_from_path(DEFAULT_MODULE_SUMMARY_CSV)

	functionality_rows = await read_csv_rows_from_upload(functionality_summary_csv)
	if not functionality_rows:
		functionality_rows = read_csv_rows_from_path(DEFAULT_FUNCTIONALITY_SUMMARY_CSV)

	feature_rows = await read_csv_rows_from_upload(feature_module_csv)
	if not feature_rows:
		feature_rows = read_csv_rows_from_path(DEFAULT_FEATURE_MODULE_CSV)

	dependency_rows = await read_csv_rows_from_upload(dependency_metrics_csv)
	modified_rows = await read_csv_rows_from_upload(modified_functions_csv)
	ticket_rows = read_csv_rows_from_path(DEFAULT_TICKET_EXPORT_CSV)

	if not module_rows or not functionality_rows or not feature_rows:
		raise HTTPException(
			status_code=400,
			detail=(
				"Required baseline CSV data is missing. Provide uploads or ensure these files exist: "
				"bug_ticket_feature_module_recurrence_summary.csv, "
				"bug_ticket_functionality_summary.csv, "
				"msp_feature_module_mapping.csv"
			),
		)

	result = compute_predictive_outputs(
		module_summary_rows=module_rows,
		functionality_summary_rows=functionality_rows,
		feature_module_rows=feature_rows,
		dependency_rows=dependency_rows if include_dependencies else [],
		modified_function_rows=modified_rows,
		ticket_rows=ticket_rows,
		output_csv_path=DEFAULT_RECURRENCE_OUTPUT_CSV,
		recent_window_days=DEFAULT_RECENT_WINDOW_DAYS,
		top_score_levels=max(1, min(top_score_levels, 500)),
	)

	result["run_meta"] = {
		"release_name": release_name,
		"days": days,
		"work_item_scope": work_item_scope,
		"priority_scope": priority_scope,
		"match_mode": match_mode,
		"include_dependencies": include_dependencies,
		"top_score_levels": max(1, min(top_score_levels, 500)),
		"ran_at": datetime.now(timezone.utc).isoformat(),
	}
	return result


async def run_predictive_end_to_end_service(
	release_name: str,
	days: int,
	top_score_levels: int,
	work_item_scope: str,
	priority_scope: str,
	match_mode: str,
	include_dependencies: bool,
	refresh_ado_export: bool,
	apply_query_update: bool,
	msp_sheet_name: str,
	msp_target_category: str,
	run_id: str,
	msp_workbook: UploadFile | None,
	dependency_metrics_csv: UploadFile | None,
	modified_functions_csv: UploadFile | None,
) -> dict[str, object]:
	run_id = run_id.strip() or str(uuid4())
	set_predictive_run_status(run_id, "running", "validation", "Validating inputs")

	if msp_workbook is None:
		set_predictive_run_status(run_id, "failed", "validation", "MSP workbook is required")
		raise HTTPException(status_code=400, detail="MSP workbook is required for end-to-end run.")

	workbook_name = msp_workbook.filename or "uploaded_msp.xlsx"
	workbook_suffix = Path(workbook_name).suffix or ".xlsx"
	if workbook_suffix.lower() not in {".xlsx", ".xlsm", ".xls"}:
		set_predictive_run_status(run_id, "failed", "validation", "MSP workbook extension is invalid")
		raise HTTPException(status_code=400, detail="MSP workbook must be .xlsx, .xlsm, or .xls")

	workbook_bytes = await msp_workbook.read()
	if not workbook_bytes:
		set_predictive_run_status(run_id, "failed", "validation", "Uploaded MSP workbook is empty")
		raise HTTPException(status_code=400, detail="Uploaded MSP workbook is empty.")

	stage_logs: dict[str, str] = {}
	with NamedTemporaryFile(suffix=workbook_suffix, delete=False) as tmp_file:
		tmp_file.write(workbook_bytes)
		tmp_workbook_path = Path(tmp_file.name)

	try:
		set_predictive_run_status(run_id, "running", "msp_feature_module_mapper", "Generating MSP feature-module mapping")
		mapper_cmd = [
			sys.executable,
			str(ROOT_DIR / "msp_feature_module_mapper.py"),
			"--workbook", str(tmp_workbook_path),
			"--sheet-name", msp_sheet_name,
			"--target-category", msp_target_category,
			"--output", str(DEFAULT_FEATURE_MODULE_CSV),
			"--module-summary-output", str(ROOT_DIR / "msp_module_summary.csv"),
			"--log-level", "ERROR",
		]
		stage_logs["msp_feature_module_mapper"] = await asyncio.to_thread(run_python_command, mapper_cmd, "MSP feature-module mapping")

		if refresh_ado_export:
			set_predictive_run_status(run_id, "running", "ado_export", "Refreshing ADO export CSV")
			ado_cmd = [sys.executable, str(ROOT_DIR / "create_poc_ado_query.py"), "--export-csv", str(DEFAULT_TICKET_EXPORT_CSV)]
			if apply_query_update:
				ado_cmd.extend(["--apply", "--update-if-exists"])
			stage_logs["ado_export"] = await asyncio.to_thread(run_python_command, ado_cmd, "ADO ticket export")
		else:
			set_predictive_run_status(run_id, "running", "ado_export", "Skipping ADO export refresh (using existing CSV)")

		set_predictive_run_status(run_id, "running", "combine_mapping", "Building combined mapping and summaries")
		combine_cmd = [
			sys.executable,
			str(ROOT_DIR / "combine_bug_feature_module_mapping.py"),
			"--bug-ticket-csv", str(DEFAULT_TICKET_EXPORT_CSV),
			"--feature-csv", str(DEFAULT_FEATURE_MODULE_CSV),
			"--output-csv", str(ROOT_DIR / "bug_ticket_feature_module_mapping.csv"),
			"--summary-csv", str(DEFAULT_MODULE_SUMMARY_CSV),
			"--functionality-summary-csv", str(DEFAULT_FUNCTIONALITY_SUMMARY_CSV),
		]
		stage_logs["combine_mapping"] = await asyncio.to_thread(run_python_command, combine_cmd, "Combined mapping generation")

		set_predictive_run_status(run_id, "running", "load_inputs", "Loading generated CSV inputs")
		module_rows = read_csv_rows_from_path(DEFAULT_MODULE_SUMMARY_CSV)
		functionality_rows = read_csv_rows_from_path(DEFAULT_FUNCTIONALITY_SUMMARY_CSV)
		feature_rows = read_csv_rows_from_path(DEFAULT_FEATURE_MODULE_CSV)
		ticket_rows = read_csv_rows_from_path(DEFAULT_TICKET_EXPORT_CSV)
		if not module_rows or not functionality_rows or not feature_rows:
			raise HTTPException(status_code=500, detail="End-to-end run did not generate expected intermediate CSV outputs.")

		dependency_rows = await read_csv_rows_from_upload(dependency_metrics_csv)
		provided_modified_rows = await read_csv_rows_from_upload(modified_functions_csv)
		modified_rows = provided_modified_rows or derive_modified_functions_from_feature_rows(feature_rows)

		set_predictive_run_status(run_id, "running", "predictive_scoring", "Computing recurrence scoring and top candidates")
		result = compute_predictive_outputs(
			module_summary_rows=module_rows,
			functionality_summary_rows=functionality_rows,
			feature_module_rows=feature_rows,
			dependency_rows=dependency_rows if include_dependencies else [],
			modified_function_rows=modified_rows,
			ticket_rows=ticket_rows,
			output_csv_path=DEFAULT_RECURRENCE_OUTPUT_CSV,
			recent_window_days=DEFAULT_RECENT_WINDOW_DAYS,
			top_score_levels=max(1, min(top_score_levels, 500)),
		)
		set_predictive_run_status(run_id, "running", "finalizing", "Preparing final results")
	except HTTPException as exc:
		set_predictive_run_status(run_id, "failed", "failed", str(exc.detail)[:300])
		raise
	except Exception as exc:
		set_predictive_run_status(run_id, "failed", "failed", str(exc)[:300])
		raise
	finally:
		try:
			tmp_workbook_path.unlink(missing_ok=True)
		except Exception:
			pass

	result["run_meta"] = {
		"release_name": release_name,
		"days": days,
		"work_item_scope": work_item_scope,
		"priority_scope": priority_scope,
		"match_mode": match_mode,
		"include_dependencies": include_dependencies,
		"top_score_levels": max(1, min(top_score_levels, 500)),
		"refresh_ado_export": refresh_ado_export,
		"apply_query_update": apply_query_update,
		"msp_sheet_name": msp_sheet_name,
		"msp_target_category": msp_target_category,
		"run_id": run_id,
		"ran_at": datetime.now(timezone.utc).isoformat(),
	}
	result["stage_logs"] = {name: (content[-800:] if content else "") for name, content in stage_logs.items()}
	result["outputs"]["ticket_export_csv"] = str(DEFAULT_TICKET_EXPORT_CSV)
	result["outputs"]["feature_module_csv"] = str(DEFAULT_FEATURE_MODULE_CSV)
	set_predictive_run_status(run_id, "completed", "completed", "Pipeline completed successfully")
	return result


def get_run_status(run_id: str) -> dict[str, object]:
	run = PREDICTIVE_RUN_STATUS.get(run_id)
	if run is None:
		raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
	return run
