from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import csv
import html
from dataclasses import dataclass
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
DOWNLOADS_DIR = ROOT / "downloads"
FINAL_OUTPUT_PATH = DOWNLOADS_DIR / "tracking_upload_template.txt"
UNMAPPED_COURIERS_PATH = DOWNLOADS_DIR / "unmapped_courier_services.csv"

STAGE_SCRIPTS = [
    ("Stage 0", ROOT / "automation_stage00.py"),
    ("Stage 1", ROOT / "automation_stage01.py"),
    ("Stage 2", ROOT / "automation_stage02.py"),
]

LOG_PREFIX_RE = re.compile(r"^\[(?P<level>[A-Z]+)\]\s*(?P<message>.*)$")
STEP_RE = re.compile(r"(Step\s+[0-9]+(?:\.[0-9]+)*[^:]*)")


@dataclass
class StageResult:
    name: str
    return_code: int
    logs: list[str]
    elapsed_seconds: float


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def parse_log_line(line: str) -> tuple[str, str, str]:
    match = LOG_PREFIX_RE.match(line)
    if match:
        level = match.group("level")
        message = match.group("message")
    else:
        level = "LOG"
        message = line

    step_match = STEP_RE.search(message)
    step = step_match.group(1) if step_match else ""
    return level, step, message


def render_log_panel(log_box, lines: list[str]) -> None:
    rendered_lines = "\n".join(
        f"<div>{html.escape(line)}</div>" for line in lines[-250:]
    )
    log_box.markdown(
        f"""
        <div style="
            height: 350px;
            overflow-y: auto;
            border-radius: 8px;
            background: #0e1117;
            border: 1px solid rgba(250, 250, 250, 0.12);
                padding: 16px;
        ">
            <div style="
                margin: 0;
                white-space: normal;
                word-break: break-word;
                color: #fafafa;
                font-size: 0.9rem;
                line-height: 1.45;
                font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas,
                    'Liberation Mono', 'Courier New', monospace;
            ">{rendered_lines}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def run_stage(
    stage_name: str,
    script_path: Path,
    log_lines: list[str],
    stage_status,
    step_status,
    uptime_status,
    log_box,
) -> StageResult:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["HEADLESS"] = "true"
    env["AUTOMATION_HEADLESS"] = "true"

    stage_start = time.monotonic()
    stage_status.info(f"{stage_name} running")
    step_status.info("Waiting for first log line... Headless browser mode is active.")

    process = subprocess.Popen(
        [sys.executable, str(script_path)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )

    stage_logs: list[str] = []

    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.rstrip()
        if not line:
            continue

        elapsed = time.monotonic() - stage_start
        level, step, message = parse_log_line(line)
        formatted_line = f"{stage_name} | {line}"

        stage_logs.append(line)
        log_lines.append(formatted_line)

        stage_status.info(f"{stage_name} running")
        step_status.info(message)
        uptime_status.metric("Current stage uptime", format_duration(elapsed))
        render_log_panel(log_box, log_lines)

        if level == "WARN":
            st.toast(message)

    return_code = process.wait()
    elapsed_seconds = time.monotonic() - stage_start

    if return_code == 0:
        stage_status.success(
            f"{stage_name} completed in {format_duration(elapsed_seconds)}"
        )
    else:
        stage_status.error(
            f"{stage_name} failed after {format_duration(elapsed_seconds)}"
        )

    return StageResult(stage_name, return_code, stage_logs, elapsed_seconds)


def generated_files() -> list[Path]:
    if not DOWNLOADS_DIR.exists():
        return []
    return sorted(
        [path for path in DOWNLOADS_DIR.iterdir() if path.is_file()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def render_generated_files() -> None:
    st.subheader("Generated Files")
    files = generated_files()
    if not files:
        st.caption("No files found in downloads yet.")
        return

    rows = []
    for path in files:
        stat = path.stat()
        rows.append(
            {
                "file": path.name,
                "size_kb": round(stat.st_size / 1024, 1),
                "modified": time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.localtime(stat.st_mtime),
                ),
            }
        )
    st.dataframe(rows, hide_index=True, use_container_width=True)


def render_downloads() -> None:
    st.subheader("Final Output")
    if FINAL_OUTPUT_PATH.exists():
        st.success(f"Final output ready: {FINAL_OUTPUT_PATH}")
        upload_stats = tracking_upload_stats(FINAL_OUTPUT_PATH)
        if upload_stats["rows"]:
            st.caption(
                "Rows: {rows} | Complete tracking rows: {complete_rows} | "
                "Rows missing tracking/date/carrier: {incomplete_rows}".format(
                    **upload_stats
                )
            )
        if upload_stats["incomplete_rows"]:
            st.warning(
                "The final output contains rows with blank tracking/date/carrier fields. "
                "Review these before using the file."
            )
        st.download_button(
            "Download tracking upload file",
            data=FINAL_OUTPUT_PATH.read_bytes(),
            file_name=FINAL_OUTPUT_PATH.name,
            mime="text/plain",
        )

    else:
        st.info("Final output is not available yet. Click 'Click to Start' first.")

    unmapped_rows = unmapped_courier_rows(UNMAPPED_COURIERS_PATH)
    if unmapped_rows:
        st.warning(
            "Unmapped courier review file exists. Check it before using the final output."
        )
        st.download_button(
            "Download unmapped courier review",
            data=UNMAPPED_COURIERS_PATH.read_bytes(),
            file_name=UNMAPPED_COURIERS_PATH.name,
            mime="text/csv",
        )
    elif UNMAPPED_COURIERS_PATH.exists():
        st.success("No unmapped courier services found.")


def tracking_upload_stats(path: Path) -> dict[str, int]:
    if not path.exists():
        return {"rows": 0, "complete_rows": 0, "incomplete_rows": 0}

    with path.open(newline="", encoding="utf-8-sig") as file:
        rows = list(csv.DictReader(file, delimiter="\t"))

    required_columns = [
        "Tracking Number",
        "Date Shipped",
        "Shipping Carrier Code",
        "Shipping Class Code",
    ]
    complete_rows = sum(
        1
        for row in rows
        if all(str(row.get(column, "") or "").strip() for column in required_columns)
    )
    return {
        "rows": len(rows),
        "complete_rows": complete_rows,
        "incomplete_rows": len(rows) - complete_rows,
    }


def cancelled_tracking_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []

    with path.open(newline="", encoding="utf-8-sig") as file:
        rows = list(csv.DictReader(file, delimiter="\t"))

    cancelled_rows = []
    for row in rows:
        has_cancelled_value = any(
            str(row.get(column, "") or "").strip().upper() == "CANCELLED"
            for column in (
                "Tracking Number",
                "Shipping Carrier Code",
                "Shipping Class Code",
            )
        )
        prevent_processing = (
            str(row.get("Prevent Site Processing", "") or "").strip().upper() == "TRUE"
        )
        if has_cancelled_value or prevent_processing:
            cancelled_rows.append(row)

    return cancelled_rows


def render_cancelled_tracking_rows(path: Path) -> None:
    rows = cancelled_tracking_rows(path)
    if not rows:
        st.success("No cancelled rows found in the tracking upload file.")
        return

    st.warning(f"Cancelled rows found: {len(rows)}")
    visible_columns = [
        "Invoice No",
        "Tracking Number",
        "Date Shipped",
        "Shipping Carrier Code",
        "Shipping Class Code",
        "Prevent Site Processing",
    ]
    st.dataframe(
        [{column: row.get(column, "") for column in visible_columns} for row in rows],
        hide_index=True,
        use_container_width=True,
    )


def unmapped_courier_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as file:
        return [
            row
            for row in csv.DictReader(file)
            if str(row.get("Shipping Carrier Source", "") or "").strip()
        ]



def main() -> None:
    st.set_page_config(
        page_title="Mark Orders Shipped Automation",
        layout="wide",
    )
    if "automation_running" not in st.session_state:
        st.session_state.automation_running = False

    st.title("Mark Orders Shipped Automation")
    st.caption("Generates the final upload handoff file.")

    left, right = st.columns([2, 1])

    with right:
        st.subheader("Run Controls")
        run_button_slot = st.empty()
        run_button = run_button_slot.button(
            "Running..." if st.session_state.automation_running else "Click to Start",
            type="primary",
            use_container_width=True,
            disabled=st.session_state.automation_running,
            key="start_automation_button",
        )
        st.caption("Keep this browser tab open while the automation is running.")
        final_output_slot = st.empty()
        generated_files_slot = st.empty()
        run_summary_slot = st.empty()
        with final_output_slot.container():
            render_downloads()
        with generated_files_slot.container():
            render_generated_files()

    with left:
        render_cancelled_tracking_rows(FINAL_OUTPUT_PATH)
        st.subheader("Run Status")
        workflow_status = st.empty()
        stage_status = st.empty()
        step_status = st.empty()
        uptime_status = st.empty()
        log_box = st.empty()

        run_requested = run_button

        if not run_requested:
            workflow_status.info("Idle")
            step_status.info("No run started yet.")
            render_log_panel(log_box, ["Logs will appear here after you start a run."])
            return

        st.session_state.automation_running = True
        run_button_slot.button(
            "Running...",
            type="primary",
            use_container_width=True,
            disabled=True,
            key="running_automation_button",
        )

        workflow_start = time.monotonic()
        log_lines: list[str] = []
        results: list[StageResult] = []

        workflow_status.info("Automation running")
        stages_to_run = STAGE_SCRIPTS

        try:
            for stage_name, script_path in stages_to_run:
                result = run_stage(
                    stage_name,
                    script_path,
                    log_lines,
                    stage_status,
                    step_status,
                    uptime_status,
                    log_box,
                )
                results.append(result)
                if result.return_code != 0:
                    workflow_status.error(f"Stopped because {stage_name} failed.")
                    break
            else:
                total_elapsed = time.monotonic() - workflow_start
                workflow_status.success(
                    " + ".join(stage_name for stage_name, _ in stages_to_run)
                    + f" completed in {format_duration(total_elapsed)}"
                )
                uptime_status.metric("Total uptime", format_duration(total_elapsed))
        finally:
            st.session_state.automation_running = False
            run_button_slot.button(
                "Click to Start",
                type="primary",
                use_container_width=True,
                disabled=False,
                key="restart_automation_button",
            )

        with run_summary_slot.container():
            with st.expander("Run Summary", expanded=True):
                for result in results:
                    status = "completed" if result.return_code == 0 else "failed"
                    st.write(
                        f"{result.name}: {status} in "
                        f"{format_duration(result.elapsed_seconds)}"
                    )

        with final_output_slot.container():
            render_downloads()
        with generated_files_slot.container():
            render_generated_files()


if __name__ == "__main__":
    main()
