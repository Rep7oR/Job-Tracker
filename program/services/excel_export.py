from __future__ import annotations

from pathlib import Path


def export_applied_jobs_xlsx(rows: list[dict], output: Path) -> Path:
    """Create the local application tracker using XlsxWriter.

    Kept dependency-light so the JobFlow app does not require artifact_tool at runtime.
    """
    import xlsxwriter

    output.parent.mkdir(parents=True, exist_ok=True)
    workbook = xlsxwriter.Workbook(str(output))
    sheet = workbook.add_worksheet("Applied Jobs")
    dashboard = workbook.add_worksheet("Summary")

    headers = [
        "Job Title", "Company", "Location", "Applied Date", "Status",
        "Job URL", "CV Used", "Cover Letter", "Industry", "Experience",
        "Work Type", "Salary", "Source",
    ]
    header_fmt = workbook.add_format({
        "bold": True, "font_color": "white", "bg_color": "#18212F",
        "border": 0, "align": "center", "valign": "vcenter",
    })
    text_fmt = workbook.add_format({"valign": "top", "text_wrap": True})
    date_fmt = workbook.add_format({"valign": "top", "num_format": "yyyy-mm-dd"})
    link_fmt = workbook.add_format({"font_color": "#2563EB", "underline": 1})

    for col, header in enumerate(headers):
        sheet.write(0, col, header, header_fmt)
    for r_idx, r in enumerate(rows, start=1):
        vals = [
            r.get("title", ""), r.get("company", ""), r.get("location", ""),
            r.get("applied_date", ""), r.get("status", "Applied"), r.get("url", ""),
            r.get("cv_path", ""), r.get("cover_letter_path", ""), r.get("industry", ""),
            r.get("experience", ""), r.get("work_type", ""), r.get("salary", ""),
            r.get("source", ""),
        ]
        for c_idx, val in enumerate(vals):
            fmt = link_fmt if c_idx == 5 and val else text_fmt
            if c_idx == 5 and val:
                sheet.write_url(r_idx, c_idx, val, fmt, string=val)
            else:
                sheet.write(r_idx, c_idx, val, fmt)

    widths = [30, 24, 24, 14, 16, 48, 55, 55, 24, 20, 18, 20, 16]
    for idx, width in enumerate(widths):
        sheet.set_column(idx, idx, width)
    sheet.freeze_panes(1, 0)
    sheet.autofilter(0, 0, max(0, len(rows)), len(headers) - 1)

    # Status summary sheet.
    from collections import Counter
    counts = Counter((r.get("status") or "Applied") for r in rows)
    dashboard.write("A1", "JobFlow Application Summary", workbook.add_format({"bold": True, "font_size": 16}))
    dashboard.write("A3", "Metric", header_fmt)
    dashboard.write("B3", "Value", header_fmt)
    metrics = [
        ("Total applications", len(rows)),
        ("Applied", counts.get("Applied", 0)),
        ("Shortlisted", counts.get("Shortlisted", 0)),
        ("Interview", counts.get("Interview", 0)),
        ("Offer", counts.get("Offer", 0)),
        ("Rejected", counts.get("Rejected", 0)),
    ]
    for i, (name, val) in enumerate(metrics, start=3):
        dashboard.write(i, 0, name, text_fmt)
        dashboard.write(i, 1, val, text_fmt)
    dashboard.set_column("A:A", 24)
    dashboard.set_column("B:B", 14)
    if counts:
        chart = workbook.add_chart({"type": "column"})
        chart.add_series({
            "name": "Applications",
            "categories": ["Summary", 3, 0, 3 + len(metrics) - 1, 0],
            "values": ["Summary", 3, 1, 3 + len(metrics) - 1, 1],
        })
        chart.set_title({"name": "Application status"})
        chart.set_legend({"none": True})
        dashboard.insert_chart("D3", chart, {"x_scale": 1.2, "y_scale": 1.1})

    workbook.close()
    return output
