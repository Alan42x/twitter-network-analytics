"""Build matching English PDF and Markdown reports from current execution evidence."""
import argparse
import ast
import gzip
import html
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image as PILImage, ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
                               Image, PageBreak)
from reportlab.graphics.shapes import Drawing, Rect, String, Line, Polygon
from reportlab.graphics import renderSVG

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app import Snapshot
from scripts.evidence import project_sources, sha256, sources_match
EVIDENCE = ROOT / "artifacts/evidence"
ASSETS = ROOT / "report/assets"
INK = colors.HexColor("#111111")
ACCENT = colors.HexColor("#202020")
GRAY = colors.HexColor("#555555")
LINE = colors.HexColor("#D4D4D4")
PALE = colors.HexColor("#F2F2F2")
PAGE_WIDTH, PAGE_HEIGHT = 595.28, 841.89
WIDTH = PAGE_WIDTH - 104


def code_image(source, start, end, destination):
    lines = Path(source).read_text().splitlines()[start - 1:end]
    font_path = Path("/System/Library/Fonts/Menlo.ttc")
    alternatives = [font_path, Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")]
    font = next((ImageFont.truetype(str(path), 22) for path in alternatives if path.exists()),
                ImageFont.load_default(size=22))
    heading = next((ImageFont.truetype(str(path), 24) for path in alternatives if path.exists()), font)
    width = max(1000, int(max(font.getlength(line) for line in lines) + 130))
    image = PILImage.new("RGB", (width, 82 + len(lines) * 31), "#f7f7f7")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width, 64), fill="#202020")
    draw.text((25, 18), f"{Path(source).relative_to(ROOT)}  |  lines {start}-{end}", fill="white", font=heading)
    for index, line in enumerate(lines):
        y = 78 + index * 31
        draw.text((18, y), str(start + index).rjust(2), fill="#777777", font=font)
        draw.text((78, y), line, fill="#111111", font=font)
    image.save(destination)


def fitted_image(path, width=WIDTH, max_height=600):
    with PILImage.open(path) as image:
        w, h = image.size
    scale = min(width / w, max_height / h)
    return Image(str(path), width=w * scale, height=h * scale, hAlign="LEFT")


def architecture():
    drawing = Drawing(WIDTH, 152)
    def box(x, y, width, title, subtitle):
        drawing.add(Rect(x, y, width, 32, rx=5, fillColor=PALE, strokeColor=LINE))
        drawing.add(String(x + width/2, y + 20, title, textAnchor="middle", fontName="Helvetica-Bold", fontSize=8.5, fillColor=INK))
        drawing.add(String(x + width/2, y + 8, subtitle, textAnchor="middle", fontName="Helvetica", fontSize=6.7, fillColor=GRAY))
    def arrow(x1, y1, x2, y2):
        drawing.add(Line(x1, y1, x2, y2, strokeColor=ACCENT, strokeWidth=1))
        if y1 == y2:
            drawing.add(Polygon([x2, y2, x2-5, y2+3, x2-5, y2-3], fillColor=ACCENT, strokeColor=ACCENT))
        else:
            base = y2 - 5 if y2 > y1 else y2 + 5
            drawing.add(Polygon([x2, y2, x2-3, base, x2+3, base], fillColor=ACCENT, strokeColor=ACCENT))
    box(0, 110, 105, "Input S3", "dataset + PySpark script")
    box(122, 110, 108, "EMR on EC2", "Spark / YARN, 2 nodes")
    box(247, 110, 116, "Run output in S3", "JSON parts + _SUCCESS")
    box(380, 110, WIDTH-380, "Controller", "validate + publish")
    arrow(105, 126, 122, 126); arrow(230, 126, 247, 126); arrow(363, 126, 380, 126)
    box(0, 60, 87, "Browser", "HTML / CSS / JS")
    box(104, 60, 98, "API Gateway", "HTTPS + path routing")
    box(219, 60, 127, "API Lambda", "/api/* + snapshot cache")
    box(363, 60, WIDTH-363, "Private result S3", "validated publication")
    arrow(87, 76, 104, 76); arrow(202, 76, 219, 76); arrow(346, 76, 363, 76)
    arrow(435, 110, 435, 92)
    box(219, 10, 127, "Website Lambda", "/ + asset allowlist")
    box(363, 10, WIDTH-363, "Private frontend S3", "HTML / CSS / JS")
    drawing.add(Line(153, 60, 153, 26, strokeColor=ACCENT, strokeWidth=1))
    arrow(153, 26, 219, 26); arrow(346, 26, 363, 26)
    return drawing


def cloud_status(snapshot):
    missing = []
    provenance = snapshot.provenance
    job_path = EVIDENCE / "cloud-job.json"
    if provenance.get("environment") != "cloud":
        missing.append("Real EMR on EC2 execution and publication")
    if not job_path.exists():
        missing.append("Successful AWS job record")
    else:
        job = read_record("cloud-job.json")
        step = job.get("step", {})
        if step.get("Status", {}).get("State") != "COMPLETED" or step.get("Id") != provenance.get("job_run_id"):
            missing.append("Matching COMPLETED step and step ID")
        if job.get("cluster", {}).get("Id") != provenance.get("cluster_id"):
            missing.append("Matching cluster ID")
    required = ["aws-emr-success.png", "aws-s3-output.png"]
    for name in required:
        if not (ASSETS / name).exists():
            missing.append(name)
        elif name.startswith("aws-") and not manual_capture_matches(name, snapshot):
            missing.append("Reviewed screenshot for the current run: " + name)
    browser_path = EVIDENCE / "browser-verification.json"
    if browser_path.exists():
        browser = read_record("browser-verification.json")
        if not valid_record(browser, snapshot.snapshot_id) or browser.get("environment") != "cloud" or browser.get("provenance") != provenance or not str(browser.get("url", "")).startswith("https://"):
            missing.append("HTTPS cloud frontend verification")
    else:
        missing.append("Cloud frontend verification record")
    cloud_summary = EVIDENCE / "cloud-summary.json"
    if cloud_summary.exists():
        evidence = read_record("cloud-summary.json")
        if evidence.get("summary") != snapshot.summary or evidence.get("provenance") != provenance or not valid_record({**evidence, "status": "PASS"}, snapshot.snapshot_id):
            missing.append("Matching cloud summary, provenance and current source")
    else:
        missing.append("Cloud summary and input checksum")
    if not captures_match(snapshot):
        missing.append("Matching report images and browser verification")
    return list(dict.fromkeys(missing))


def read_record(name):
    try:
        record = json.loads((EVIDENCE / name).read_text())
        return record if isinstance(record, dict) else {}
    except (OSError, ValueError):
        return {}


def valid_record(record, snapshot_id=None, expected=None):
    return isinstance(record, dict) and record.get("status") == "PASS" and sources_match(record, expected) and (
        snapshot_id is None or record.get("snapshot_id") == snapshot_id)


def captures_match(snapshot):
    record = read_record("browser-verification.json")
    captures = record.get("captures", {})
    if not isinstance(captures, dict) or not isinstance(captures.get("images"), dict):
        return False
    names = tuple(captures["images"].get(role) for role in ("frontend", "api"))
    if names not in (("manual-frontend.png", "manual-api.png"), ("frontend-results.png", "backend-api.png")):
        return False
    return (valid_record(record, snapshot.snapshot_id) and record.get("provenance") == snapshot.provenance
            and isinstance(captures.get("files"), dict) and all(
                (ASSETS / name).is_file() and sha256(ASSETS / name) == captures["files"].get(name)
                and (not name.startswith("manual-") or manual_capture_matches(name, snapshot))
                for name in names))


def manual_capture_matches(name, snapshot):
    record = read_record("manual-captures.json")
    path = ASSETS / name
    return (record.get("review_status") == "REVIEWED" and sources_match(record)
            and record.get("snapshot_id") == snapshot.snapshot_id
            and record.get("provenance") == snapshot.provenance
            and record.get("url") == read_record("browser-verification.json").get("url")
            and path.is_file() and isinstance(record.get("files"), dict)
            and record["files"].get(name) == sha256(path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--student-id")
    parser.add_argument("--student-name")
    args = parser.parse_args()
    content = json.loads((ROOT / "report/content.json").read_text())
    student_id = args.student_id or content["student_id"]
    student_name = args.student_name or content["student_name"]
    if not isinstance(student_id, str) or not student_id.isascii() or not student_id.isalnum():
        raise ValueError("Use an alphanumeric student ID for the submission filename")
    if not isinstance(student_name, str) or not student_name.strip():
        raise ValueError("Student name is required")
    for field in ("healthcare", "bank"):
        if not isinstance(content.get(field), list) or not content[field] or any(
                not isinstance(text, str) or not text.strip() for text in content[field]):
            raise ValueError(f"{field} must contain nonempty answer paragraphs")
    for field in ("frontend_setup", "backend_setup", "analysis_description"):
        if not isinstance(content.get(field), str) or not content[field].strip():
            raise ValueError(f"{field} is required")
    for field, limit in [("frontend_setup", 50), ("backend_setup", 100)]:
        if len(content[field].split()) > limit:
            raise ValueError(f"{field} exceeds {limit} words")
    snapshot = json.loads(gzip.decompress((ROOT / "artifacts/current.json.gz").read_bytes()))
    validated = Snapshot(snapshot)
    local = read_record("local-verification.json")
    browser = read_record("browser-verification.json")
    checks = read_record("project-checks.json")
    local_valid = valid_record(local, validated.snapshot_id)
    browser_valid = captures_match(validated)
    checks_valid = valid_record(checks, expected=project_sources())
    missing = cloud_status(validated)
    if not local_valid:
        missing.append("Current independent full-dataset verification")
    if not checks_valid:
        missing.append("Current Python and static validation")
    edge = read_record("spark-edge-verification.json")
    edge_valid = valid_record(edge)
    if not edge_valid:
        missing.append("Current Spark edge-case verification")
    draft = bool(missing)
    report = ROOT / "report"
    report.mkdir(exist_ok=True)
    ASSETS.mkdir(exist_ok=True)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    tree = ast.parse((ROOT / "analysis/spark_job.py").read_text())
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    source_start = tree.body[0].end_lineno + 1 if ast.get_docstring(tree) else 1
    code_image(ROOT / "analysis/spark_job.py", source_start, functions["compute"].end_lineno, ASSETS / "spark-compute-source.png")
    code_image(ROOT / "analysis/spark_job.py", functions["main"].lineno,
               len((ROOT / "analysis/spark_job.py").read_text().splitlines()), ASSETS / "spark-entry-source.png")
    lines = (ROOT / "frontend/app.js").read_text().splitlines()
    start = next(i for i, line in enumerate(lines, 1) if "const response = await fetch" in line)
    end = next(i for i, line in enumerate(lines, 1) if i >= start and "return content;" in line)
    code_image(ROOT / "frontend/app.js", start, end, ASSETS / "frontend-fetch-source.png")
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("BodyLab", fontName="Helvetica", fontSize=10.2, leading=14.5,
                              textColor=INK, spaceAfter=9))
    styles.add(ParagraphStyle("SmallLab", fontName="Helvetica", fontSize=8.2, leading=11.5,
                              textColor=GRAY, spaceAfter=7))
    styles.add(ParagraphStyle("TableHeaderLab", parent=styles["SmallLab"],
                              fontName="Helvetica-Bold", textColor=INK))
    styles.add(ParagraphStyle("HeadingLab", fontName="Helvetica-Bold", fontSize=13.5, leading=18,
                              textColor=ACCENT, spaceBefore=12, spaceAfter=9, keepWithNext=True))
    styles.add(ParagraphStyle("TitleLab", fontName="Helvetica-Bold", fontSize=23, leading=27,
                              textColor=INK, spaceAfter=10))
    body = styles["BodyLab"]
    small = styles["SmallLab"]
    heading = styles["HeadingLab"]
    story = []
    markdown = []

    def p(text, style=body):
        return Paragraph(html.escape(text), style)

    def add(text, style=body, level=None):
        story.append(p(text, style))
        if level is None:
            level = 2 if style is styles["TitleLab"] else 3 if style is heading else 0
        markdown.append("#" * level + " " + text if level else text)

    def add_image(path, width=WIDTH, max_height=600):
        story.append(fitted_image(path, width=width, max_height=max_height))
        relative = path.relative_to(report).as_posix()
        markdown.append(f"![{path.stem.replace('-', ' ')}]({relative})")

    def table(rows, widths):
        cells = [[p(str(cell), styles["TableHeaderLab"] if index == 0 else small) for cell in row]
                 for index, row in enumerate(rows)]
        result = Table(cells, colWidths=widths, hAlign="LEFT")
        result.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), PALE),
            ("TEXTCOLOR", (0, 0), (-1, 0), ACCENT),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LINEBELOW", (0, 0), (-1, 0), .7, LINE),
            ("LINEBELOW", (0, 1), (-1, -1), .35, LINE),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(result)
        lines = [[str(cell).replace("|", "\\|").replace("\n", "<br>") for cell in row] for row in rows]
        markdown.append("\n".join(
            ["| " + " | ".join(lines[0]) + " |", "| " + " | ".join("---" for _ in lines[0]) + " |"]
            + ["| " + " | ".join(row) + " |" for row in lines[1:]]))

    add("CS5224  /  LAB 02", small)
    add("Cloud Services & Twitter Network Analytics", styles["TitleLab"], level=1)
    add(f"{student_name}  |  {student_id}")
    if draft:
        add("DRAFT - cloud execution evidence pending.", small)
    add("Part 1(a) - Healthcare analytics SaaS", heading, level=2)
    for paragraph in content["healthcare"]:
        add(paragraph)
    add("Part 1(b) - Bank application modernisation", heading, level=2)
    for paragraph in content["bank"]:
        add(paragraph)
    story.append(PageBreak())

    add("Part 2 - Full-stack data analytics pipeline", styles["TitleLab"])
    add("Deployment architecture", heading)
    add("The deployed architecture uses API Gateway HTTP API routes and Lambda proxy integrations. [4,5]", small)
    diagram = architecture()
    story.append(diagram)
    diagram_path = ASSETS / "deployment-architecture.svg"
    renderSVG.drawToFile(diagram, str(diagram_path))
    markdown.append("![Configured AWS deployment](assets/deployment-architecture.svg)")
    add("Frontend setup", heading)
    add(content["frontend_setup"])
    add("Backend setup", heading)
    add(content["backend_setup"])
    add("Data analysis component", heading)
    add(content["analysis_description"])
    add("Semantics and data quality", heading)
    add("Each distinct row A B is a directed edge A -> B. A gains one followee and B gains one follower. IDs remain strings. Malformed/zero IDs and blank rows are audited separately. Self-follow rows are excluded as invalid social relationships; their endpoints remain in the user inventory. Duplicates are counted after excluding self-follows. All reported values are computed from the supplied, modified dataset. [6]")
    story.append(PageBreak())

    add("Results and frontend evidence", styles["TitleLab"])
    required = {row["user_id"]: row for row in snapshot["users"]}
    table([["User ID", "Followers", "Followees"],
           *[[value, required[value]["followers"], required[value]["followees"]]
             for value in ("214328887", "107830991")]], [WIDTH*.46, WIDTH*.27, WIDTH*.27])
    story.append(Spacer(1, 14))
    for value in ("214328887", "107830991"):
        statements = [f"{value} has {required[value]['followers']} followers",
                      f"{value} has {required[value]['followees']} followees"]
        story.append(Paragraph("<br/>".join(statements), small))
        markdown.append("  \n".join(statements))
    if browser_valid:
        add_image(ASSETS / browser["captures"]["images"]["frontend"], max_height=280)
        add("Figure 1. Actual browser capture of the frontend. The page displays the API-supplied counts and identifies the execution environment.", small)
    add("Dataset audit", heading)
    summary = snapshot["summary"]
    table([["Metric", "Computed value"], ["Input rows", f"{summary['raw_lines']:,}"],
           ["Duplicate / self-follow rows removed", f"{summary['duplicate_edges']:,} / {summary['self_loop_rows']:,}"],
           ["Unique edges / users", f"{summary['unique_edges']:,} / {summary['total_users']:,}"],
           ["Malformed / blank rows", f"{summary['invalid_rows']:,} / {summary['blank_rows']:,}"]], [WIDTH*.63, WIDTH*.37])
    story.append(PageBreak())

    add("Data analysis source code", styles["TitleLab"])
    add("Core transformation: analysis/spark_job.py", heading)
    add("Figure 2. Source listing rendered directly from the implemented file. The same function is used locally and by the configured EMR on EC2 job. All expensive aggregation and deduplication operations execute through Spark's distributed DataFrame engine. [7]", small)
    add_image(ASSETS / "spark-compute-source.png", max_height=550)
    add("The audit summary is small and collected on the driver. The edge and user datasets remain distributed; only the publication controller reads the finished per-user results for this 81,306-user lab dataset.", small)
    story.append(PageBreak())

    add("Execution and API integration", styles["TitleLab"])
    add("Spark entry point and output writes", heading)
    add_image(ASSETS / "spark-entry-source.png", max_height=280)
    add("Figure 3. The entry point accepts S3 input/output URIs in the cloud, writes partitioned JSON and refuses to overwrite an existing run.", small)
    add("Frontend calls the backend", heading)
    add_image(ASSETS / "frontend-fetch-source.png", max_height=100)
    add("Figure 4. The actual fetch function requests JSON from backend endpoints and surfaces non-success responses.", small)
    add("Backend API response", heading)
    if browser_valid:
        add_image(ASSETS / browser["captures"]["images"]["api"], max_height=180)
        add("Figure 5. Actual browser response from GET /api/users?ids=214328887,107830991.", small)
    story.append(PageBreak())

    add("Validation and execution record", styles["TitleLab"])
    add("Verification results", heading)
    if local_valid:
        add(f"Spark processed all {summary['raw_lines']:,} input rows using {local['run_manifest']['master']}. Every one of the {local['compared_users']:,} user records matched the independent SQLite implementation. Both degree sums equal {summary['unique_edges']:,} unique directed edges. Input checksum, current analysis source and output success markers were verified.")
    else:
        add("Full-dataset independent verification is missing or stale; rerun the current Spark pipeline and scripts/verify_results.py.")
    if browser_valid:
        target = "the HTTPS cloud application" if browser.get("environment") == "cloud" else "the local application"
        add(f"Chrome passed {len(browser['checks'])} checks against {target}, including counts, ordering, pagination, search, exact CSV contents, mobile layout, startup failure, retry, delayed requests and changed-result handling. Captures are bound to the API result and current sources.")
    else:
        add("Browser verification or matching screenshot evidence is missing or stale.")
    if checks_valid:
        add(f"{checks['python_tests']} Python regression tests passed. The source and generated CloudFormation templates passed cfn-lint.")
    else:
        add("Current Python/static validation evidence is missing or stale.")
    if edge_valid:
        add(f"Actual Spark matched SQLite for {len(edge['cases'])} edge cases: dirty rows and direction, empty input, loop-only users, CRLF/carriage returns, 20-digit IDs and invalid UTF-8/Unicode whitespace.", small)
    add("API contracts", heading)
    add("GET /api/summary returns quality metrics and provenance; GET /api/users supports bounded pages, ID-prefix search and sorting; GET /api/users/{id} returns one user. Missing users return 404, invalid parameters return 400, and unavailable results return 503.", small)
    add("Input SHA-256", heading)
    add(summary["input_sha256"], small)
    add(f"Input size: {summary['input_bytes']:,} bytes. Execution shown by current result: {snapshot['provenance']['engine']}.", small)
    add("Cloud execution status", heading)
    if draft:
        add("Matching cloud execution evidence is incomplete. The following evidence is required before submission; this report remains a draft.")
        for item in missing:
            add("Pending: " + item, small)
        add("Complete the pending evidence listed above, then rerun scripts/build_report.py. A completed cloud run can be reused: capture its genuine EMR and S3 console screens without submitting another job. Resource IDs, commands and capture instructions are provided in README.md.")
    else:
        job = json.loads((EVIDENCE / "cloud-job.json").read_text())
        completed = datetime.fromisoformat(snapshot["provenance"]["generated_at"]).astimezone(
            timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S (UTC+08:00)")
        table([["Field", "Recorded value"], ["Step state", job["step"]["Status"]["State"]], ["Step ID", job["step"]["Id"]],
               ["Cluster ID", job["cluster"]["Id"]], ["Region", snapshot["provenance"]["region"]],
               ["Step completed", completed]], [WIDTH*.3, WIDTH*.7])
        story.append(Spacer(1, 12))
        website = html.escape(browser["url"], quote=True)
        story.append(Paragraph(f'Website: <link href="{website}" color="#202020">{website}</link>', small))
        markdown.append(f"Website: [{browser['url']}]({browser['url']})")
        story.append(PageBreak())
        add("Cloud execution evidence", styles["TitleLab"])
        add("Original AWS console screenshots show the matching completed Spark step and its partitioned S3 output. The run name and output prefix identify the same execution recorded on the preceding page.", small)
        for name, caption in [("aws-emr-success.png", "Figure 6. Amazon EMR Steps: the recorded Step ID is Completed."),
                              ("aws-s3-output.png", "Figure 7. The matching S3 run directory contains _SUCCESS and partitioned JSON results.")]:
            add_image(ASSETS / name, max_height=250)
            add(caption, small)
    story.append(PageBreak())

    add("Reproducibility and references", styles["TitleLab"])
    add("Repeatable workflow", heading)
    add("The project includes the frontend, shared Python API, independent reference implementation, complete Spark entry point, edge-case fixture, AWS template, orchestration script and report generator. Immutable input/script paths and separate run directories support reproducibility. The compressed publication is validated before replacing the previous result; unsuccessful jobs do not publish partial results.")
    add("Runtime sizing", heading)
    add("The API loads and indexes the compact per-user snapshot in a 512 MB Lambda function. This is appropriate for the supplied 81,306-user graph; much larger result sets should use a partitioned query store rather than loading the full inventory per instance. The Spark computation itself remains distributed.")
    add("Sources", heading)
    for index, source in enumerate(content["sources"], 1):
        story.append(Paragraph(f"[{index}] {html.escape(source['title'])}<br/><link href=\"{html.escape(source['url'], quote=True)}\" color=\"#202020\">{html.escape(source['url'])}</link>", small))
        markdown.append(f"{index}. [{source['title']}]({source['url']})")
    add("Numerical results were computed from the modified dataset supplied with the lab. The SNAP reference documents the source collection; the actual input used for this analysis is identified by its SHA-256 checksum in this report.", small)

    def decorate(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(LINE)
        canvas.line(52, 48, PAGE_WIDTH-52, 48)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(GRAY)
        canvas.drawString(52, 33, f"{student_name}  |  {student_id}  |  CS5224 Lab 2")
        canvas.drawRightString(PAGE_WIDTH-52, 33, str(document.page))
        if draft:
            canvas.setFont("Helvetica-Bold", 7)
            canvas.setFillColor(GRAY)
            canvas.drawRightString(PAGE_WIDTH-52, PAGE_HEIGHT-27, "DRAFT - CLOUD EVIDENCE PENDING")
        canvas.restoreState()

    destination = report / f"{student_id}.pdf"
    document = SimpleDocTemplate(str(destination), pagesize=(PAGE_WIDTH, PAGE_HEIGHT),
                                 topMargin=48, bottomMargin=65, leftMargin=52, rightMargin=52,
                                 title="CS5224 Lab 2 - Cloud Services and Twitter Analytics",
                                 author=student_name, subject=f"{student_id}; {'draft' if draft else 'cloud evidence included'}")
    document.build(story, onFirstPage=decorate, onLaterPages=decorate)
    if manual_capture_matches("aws-emr-cluster.png", validated):
        markdown.extend(["## Supplementary cluster configuration",
                         "![AWS EMR cluster configuration](assets/aws-emr-cluster.png)"])
    markdown.extend(["## Appendix - Complete PySpark source",
                     "```python\n" + (ROOT / "analysis/spark_job.py").read_text().rstrip() + "\n```"])
    markdown_path = report / f"{student_id}.md"
    markdown_path.write_text("\n\n".join(markdown) + "\n", encoding="utf-8")
    (ROOT / "artifacts/report-status.json").write_text(json.dumps({
        "student_name": student_name, "student_id": student_id,
        "status": "DRAFT_CLOUD_EVIDENCE_PENDING" if draft else "CLOUD_EVIDENCE_INCLUDED",
        "missing_evidence": missing, "frontend_words": len(content["frontend_setup"].split()),
        "backend_words": len(content["backend_setup"].split()), "snapshot_id": validated.snapshot_id,
        "local_verification_current": local_valid, "browser_verification_current": browser_valid,
        "project_checks_current": checks_valid, "spark_edge_verification_current": edge_valid}, indent=2) + "\n")
    print(destination)
    print(markdown_path)
    print("Status:", "DRAFT; real cloud evidence required" if draft else "Cloud evidence included")


if __name__ == "__main__":
    main()
