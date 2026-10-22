"""Build matching English PDF and Markdown reports from current execution evidence."""
import argparse
import ast
import gzip
import html
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image as PILImage, ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
                               Image, PageBreak, KeepTogether, Flowable)
from reportlab.graphics.shapes import Drawing, Rect, String, Line, Polygon
from reportlab.graphics import renderSVG

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app import Snapshot
from scripts.evidence import project_sources, sha256, sources_match
from scripts.cloud import validate_execution
EVIDENCE = ROOT / "artifacts/evidence"
ASSETS = ROOT / "report/assets"
INK = colors.HexColor("#172B4D")
ACCENT = colors.HexColor("#006D77")
GRAY = colors.HexColor("#4C5C70")
LINE = colors.HexColor("#D9E2EC")
PALE = colors.HexColor("#F1F5F9")
PAGE_WIDTH, PAGE_HEIGHT = 595.28, 841.89
MARGIN = 48
# Platypus frames have six points of padding on each side.
WIDTH = PAGE_WIDTH - 2 * MARGIN - 12


def source_rows(lines, columns=86):
    """Soft-wrap display rows without dropping or rewriting any source characters."""
    rows = []
    for number, line in lines:
        remaining, continued = line, False
        while len(remaining) > columns - (3 if continued else 0):
            limit = columns - (3 if continued else 0)
            cut = remaining.rfind(" ", 0, limit + 1)
            if cut < limit // 2:
                # Prefer a token boundary to splitting an identifier such as getOrCreate.
                boundary = max(remaining.rfind(char, 0, limit) + 1 for char in ",.([{")
                cut = boundary if boundary >= limit // 2 else limit
            rows.append((number if not continued else None, remaining[:cut], continued))
            remaining, continued = remaining[cut:], True
        rows.append((number if not continued else None, remaining, continued))
    return rows


class SourceListing(Flowable):
    """Selectable source with line numbers and explicit visual continuation markers."""
    def __init__(self, source, start, end, title):
        Flowable.__init__(self)
        self.width = WIDTH
        self.font_size = 8.5
        self.leading = 11.3
        self.rows = source_rows(list(enumerate(source.splitlines(), 1))[start - 1:end])
        self.height = 43 + len(self.rows) * self.leading
        self.title = title

    def draw(self):
        canvas = self.canv
        canvas.setFillColor(PALE)
        canvas.roundRect(0, 0, self.width, self.height, 5, fill=1, stroke=0)
        canvas.setFillColor(INK)
        canvas.roundRect(0, self.height - 29, self.width, 29, 5, fill=1, stroke=0)
        canvas.rect(0, self.height - 29, self.width, 8, fill=1, stroke=0)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 8)
        canvas.drawString(12, self.height - 18, self.title)
        y = self.height - 42
        for number, line, continued in self.rows:
            if number is not None:
                canvas.setFillColor(GRAY)
                canvas.setFont("Helvetica", 7)
                canvas.drawRightString(26, y, str(number))
            elif continued:
                canvas.setFillColor(ACCENT)
                canvas.setFont("Helvetica", 8)
                canvas.drawRightString(29, y, "›")
            canvas.setFillColor(INK)
            canvas.setFont("Courier", self.font_size)
            x = 36 + (15 if continued else 0)
            if x + stringWidth(line, "Courier", self.font_size) > self.width - 8:
                raise ValueError("Source listing exceeds the page width")
            canvas.drawString(x, y, line)
            y -= self.leading


def code_image(source, start, end, destination):
    lines = Path(source).read_text().splitlines()
    rows = source_rows(list(enumerate(lines, 1))[start - 1:end])
    font_path = Path("/System/Library/Fonts/Menlo.ttc")
    alternatives = [font_path, Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")]
    font = next((ImageFont.truetype(str(path), 22) for path in alternatives if path.exists()),
                ImageFont.load_default(size=22))
    heading = next((ImageFont.truetype(str(path), 24) for path in alternatives if path.exists()), font)
    width = math.ceil(font.getlength("M" * 86) + 112)
    image = PILImage.new("RGB", (width, 86 + len(rows) * 29), "#f1f5f9")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width, 64), fill="#172b4d")
    draw.text((25, 18), f"{Path(source).relative_to(ROOT)}  |  lines {start}-{end}", fill="white", font=heading)
    for index, (number, line, continued) in enumerate(rows):
        y = 78 + index * 29
        draw.text((18, y), str(number).rjust(3) if number is not None else "  >", fill="#4c5c70", font=font)
        draw.text((82 + (32 if continued else 0), y), line, fill="#172b4d", font=font)
    image.save(destination)


def fitted_image(path, width=WIDTH, max_height=600):
    with PILImage.open(path) as image:
        w, h = image.size
    scale = min(width / w, max_height / h)
    return Image(str(path), width=w * scale, height=h * scale, hAlign="LEFT")


def architecture():
    drawing = Drawing(WIDTH, 174)
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
    drawing.add(String(0, 162, "BATCH ANALYTICS", fontName="Helvetica-Bold", fontSize=7.2, fillColor=ACCENT))
    drawing.add(String(0, 101, "ONLINE REQUESTS", fontName="Helvetica-Bold", fontSize=7.2, fillColor=ACCENT))
    box(0, 117, 105, "Input S3", "dataset + PySpark script")
    box(122, 117, 108, "EMR on EC2", "1 primary + 2 workers")
    box(247, 117, 116, "Run output in S3", "JSON parts + _SUCCESS")
    box(380, 117, WIDTH-380, "Controller", "validate + publish")
    arrow(105, 133, 122, 133); arrow(230, 133, 247, 133); arrow(363, 133, 380, 133)
    box(0, 60, 87, "Browser", "HTML / CSS / JS")
    box(104, 60, 98, "API Gateway", "HTTPS + path routing")
    box(219, 60, 127, "API Lambda", "/api/* + snapshot cache")
    box(363, 60, WIDTH-363, "Private result S3", "validated publication")
    arrow(87, 76, 104, 76); arrow(202, 76, 219, 76); arrow(346, 76, 363, 76)
    arrow(435, 117, 435, 92)
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
    if worker_evidence(snapshot) is None:
        missing.append("Verified completed tasks on at least two worker hosts")
    required = ["aws-emr-cluster.png", "aws-emr-success.png", "aws-s3-output.png"]
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
        for name in ("manual-frontend.png", "manual-api.png"):
            if not manual_capture_matches(name, snapshot):
                missing.append("Reviewed screenshot for the current run: " + name)
        if all(manual_capture_matches(name, snapshot) for name in ("manual-frontend.png", "manual-api.png")):
            missing.append("Browser verification bound to the reviewed frontend/API screenshots")
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


def worker_evidence(snapshot):
    record = read_record("cloud-summary.json")
    expected = ["analysis/spark_job.py", "backend/app.py", "backend/web.py", "scripts/cloud.py"]
    if (not valid_record({**record, "status": "PASS"}, snapshot.snapshot_id, expected)
            or record.get("provenance") != snapshot.provenance):
        return None
    try:
        execution = validate_execution(record.get("execution"))
    except RuntimeError:
        return None
    if (execution["application_id"] != snapshot.provenance.get("application_id")
            or execution["worker_hosts"] != snapshot.provenance.get("worker_hosts")):
        return None
    return execution


def captures_match(snapshot):
    record = read_record("browser-verification.json")
    captures = record.get("captures", {})
    if not isinstance(captures, dict) or not isinstance(captures.get("images"), dict):
        return False
    names = tuple(captures["images"].get(role) for role in ("frontend", "api"))
    if names != ("manual-frontend.png", "manual-api.png"):
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
    browser_valid = (valid_record(browser, validated.snapshot_id)
                     and browser.get("provenance") == validated.provenance)
    captures_valid = captures_match(validated)
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
    execution = worker_evidence(validated)
    report = ROOT / "report"
    report.mkdir(exist_ok=True)
    ASSETS.mkdir(exist_ok=True)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    source_path = ROOT / "analysis/spark_job.py"
    source = source_path.read_text().rstrip()
    source_lines = source.splitlines()
    tree = ast.parse(source)
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    core_start = next(i for i, line in enumerate(source_lines, 1) if line.startswith("    edges ="))
    code_image(source_path, core_start, functions["compute"].end_lineno, ASSETS / "spark-compute-source.png")
    frontend_lines = (ROOT / "frontend/app.js").read_text().splitlines()
    fetch_start = next(i for i, line in enumerate(frontend_lines, 1) if "const response = await fetch" in line)
    fetch_end = next(i for i, line in enumerate(frontend_lines, 1) if i >= fetch_start and "return content;" in line)
    code_image(ROOT / "frontend/app.js", fetch_start, fetch_end, ASSETS / "frontend-fetch-source.png")

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("BodyLab", fontName="Helvetica", fontSize=10, leading=14,
                              textColor=INK, spaceAfter=8))
    styles.add(ParagraphStyle("SmallLab", fontName="Helvetica", fontSize=8.6, leading=11.8,
                              textColor=GRAY, spaceAfter=7))
    styles.add(ParagraphStyle("CaptionLab", parent=styles["SmallLab"], fontSize=8.2,
                              leading=11.2, spaceBefore=5, spaceAfter=11))
    styles.add(ParagraphStyle("EyebrowLab", fontName="Helvetica-Bold", fontSize=8,
                              leading=11, textColor=ACCENT, spaceAfter=7))
    styles.add(ParagraphStyle("HeadingLab", fontName="Helvetica-Bold", fontSize=12,
                              leading=16, textColor=INK, spaceBefore=10, spaceAfter=7,
                              keepWithNext=True))
    styles.add(ParagraphStyle("TitleLab", fontName="Helvetica-Bold", fontSize=21,
                              leading=25, textColor=INK, spaceAfter=11, keepWithNext=True))
    styles.add(ParagraphStyle("TableLab", fontName="Helvetica", fontSize=9,
                              leading=12.2, textColor=INK))
    styles.add(ParagraphStyle("TableHeaderLab", parent=styles["TableLab"], fontName="Helvetica-Bold",
                              fontSize=8.5, textColor=ACCENT))
    body, small = styles["BodyLab"], styles["SmallLab"]
    heading, caption_style = styles["HeadingLab"], styles["CaptionLab"]
    story, markdown, sections = [], [], []

    def p(text, style=body):
        return Paragraph(html.escape(str(text)), style)

    def add(text, style=body, level=None):
        story.append(p(text, style))
        if level is None:
            level = 3 if style is heading else 0
        markdown.append("#" * level + " " + text if level else text)

    def page_title(kicker, title, first=False):
        if not first:
            story.append(PageBreak())
        add(kicker.upper(), styles["EyebrowLab"])
        paragraph = p(title, styles["TitleLab"])
        paragraph.report_section = title
        story.append(paragraph)
        markdown.append("## " + title)

    def lead(label, text):
        story.append(Paragraph(f"<b>{html.escape(label)}.</b> {html.escape(text)}", body))
        markdown.append(f"**{label}.** {text}")

    figure_number = 0

    def add_figure(path, text, max_height=600):
        nonlocal figure_number
        figure_number += 1
        caption = f"Figure {figure_number}. {text}"
        picture = fitted_image(path, max_height=max_height)
        story.append(KeepTogether([picture, p(caption, caption_style)]))
        relative = path.relative_to(report).as_posix()
        markdown.extend([f"![{text}]({relative})", caption])

    def table(rows, proportions, numeric=()):
        cells = [[p(cell, styles["TableHeaderLab"] if index == 0 else styles["TableLab"])
                  for cell in row] for index, row in enumerate(rows)]
        result = Table(cells, colWidths=[WIDTH * value for value in proportions],
                       repeatRows=1, hAlign="LEFT")
        rules = [
            ("BACKGROUND", (0, 0), (-1, 0), PALE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, 0), .7, LINE),
            ("LINEBELOW", (0, 1), (-1, -1), .35, LINE),
            ("LEFTPADDING", (0, 0), (-1, -1), 9),
            ("RIGHTPADDING", (0, 0), (-1, -1), 9),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]
        for column in numeric:
            for row in cells[1:]:
                row[column].style = ParagraphStyle("NumericCell", parent=styles["TableLab"], alignment=2)
        result.setStyle(TableStyle(rules))
        story.extend([result, Spacer(1, 7)])
        lines = [[str(cell).replace("|", "\\|").replace("\n", "<br>") for cell in row] for row in rows]
        markdown.append("\n".join(
            ["| " + " | ".join(lines[0]) + " |", "| " + " | ".join("---" for _ in lines[0]) + " |"]
            + ["| " + " | ".join(row) + " |" for row in lines[1:]]))

    summary = snapshot["summary"]
    required = {row["user_id"]: row for row in snapshot["users"]}
    job = read_record("cloud-job.json")
    matching_job = (job.get("step", {}).get("Id") == validated.provenance.get("job_run_id")
                    and job.get("step", {}).get("Status", {}).get("State") == "COMPLETED"
                    and job.get("cluster", {}).get("Id") == validated.provenance.get("cluster_id"))
    completed = datetime.fromisoformat(snapshot["provenance"]["generated_at"]).astimezone(
        timezone(timedelta(hours=8))).strftime("%d %b %Y, %H:%M:%S (UTC+08:00)")

    page_title("CS5224 Cloud Computing / Lab 02", "Cloud Services & Twitter Analytics", first=True)
    add(f"{student_name}  |  {student_id}  |  Laboratory report", small)
    if draft:
        add("DRAFT - cloud execution evidence pending.", small)
    add("Part 1(a)  Healthcare analytics SaaS", heading, level=2)
    healthcare_labels = ("Deployment", "Governance", "Scaling")
    for index, paragraph in enumerate(content["healthcare"]):
        if index < len(healthcare_labels):
            lead(healthcare_labels[index], paragraph)
        else:
            add(paragraph)
    add("Part 1(b)  Bank application modernisation", heading, level=2)
    bank_labels = ("Service and deployment", "Migration and controls")
    for index, paragraph in enumerate(content["bank"]):
        if index < len(bank_labels):
            lead(bank_labels[index], paragraph)
        else:
            add(paragraph)

    page_title("Part 2 / Full-stack analytics pipeline", "System architecture")
    add("Spark computes the graph in a batch job; the web application queries a validated publication. Browser requests do not launch Spark jobs. API Gateway provides HTTPS access to separate API and website Lambda functions. [4,5]")
    diagram = architecture()
    story.append(diagram)
    diagram_path = ASSETS / "deployment-architecture.svg"
    renderSVG.drawToFile(diagram, str(diagram_path))
    markdown.append("![AWS deployment architecture](assets/deployment-architecture.svg)")
    add("Architecture. The controller publishes only validated results; private S3 buckets hold the dataset, output and frontend assets.", caption_style)
    add("Frontend setup", heading)
    add(content["frontend_setup"])
    add("Backend setup", heading)
    add(content["backend_setup"])
    add("Data analysis component", heading)
    add(content["analysis_description"])

    page_title("Part 2.1 / Frontend", "Required user results")
    table([["User ID", "Followers", "Followees"],
           *[[value, required[value]["followers"], required[value]["followees"]]
             for value in ("214328887", "107830991")]], [.46, .27, .27], numeric=(1, 2))
    for value in ("214328887", "107830991"):
        statements = [f"{value} has {required[value]['followers']} followers",
                      f"{value} has {required[value]['followees']} followees"]
        story.append(Paragraph("<br/>".join(statements), small))
        markdown.append("  \n".join(statements))
    if captures_valid:
        add_figure(ASSETS / "manual-frontend.png",
                   "Deployed HTTPS frontend. Both required users and the dataset metrics are fetched from the backend API.", 310)
    else:
        add("Pending reviewed screenshot: manual-frontend.png", small)
    add("Dataset audit", heading)
    table([["Metric", "Computed value"], ["Input rows", f"{summary['raw_lines']:,}"],
           ["Duplicate / self-follow rows removed", f"{summary['duplicate_edges']:,} / {summary['self_loop_rows']:,}"],
           ["Unique directed edges / users", f"{summary['unique_edges']:,} / {summary['total_users']:,}"],
           ["Malformed / blank rows", f"{summary['invalid_rows']:,} / {summary['blank_rows']:,}"]], [.63, .37])

    page_title("Part 2.2 / Backend", "API integration")
    add("Frontend request handling", heading)
    add_figure(ASSETS / "frontend-fetch-source.png",
               "Implemented fetch logic requests uncached JSON and surfaces backend errors. Long source lines wrap for readability.", 200)
    add("Published API response", heading)
    if captures_valid:
        add_figure(ASSETS / "manual-api.png",
                   "HTTPS response for the two required users. The snapshot ID identifies the published result used by the frontend.", 220)
    else:
        add("Pending reviewed screenshot: manual-api.png", small)
    add("API contract", heading)
    table([["GET endpoint", "Behaviour"],
           ["/api/summary", "Dataset metrics, execution provenance and snapshot ID."],
           ["/api/users?ids=...", "Batch lookup for up to 20 IDs; reports missing IDs."],
           ["/api/users", "Pagination (1-200 rows), degree sorting and user-ID prefix search."],
           ["/api/users/{id}", "Follower and followee counts for one user."]], [.39, .61])
    add("Invalid parameters return 400; missing users return 404; a changed snapshot returns 412; unavailable results return 503. The frontend binds requests to one snapshot and offers retry after failure.", small)

    page_title("Part 2.3 / Data analysis", "Spark processing & source")
    add("For each directed relationship A → B, A gains one followee and B gains one follower. Counts use distinct cleaned edges from the modified lab dataset. [6,7]")
    table([["Stage", "Processing rule"],
           ["Parse and audit", "Read LF-delimited rows. Accept positive decimal IDs of up to 20 digits; normalise leading zeroes. Audit malformed and blank rows separately."],
           ["Clean the graph", "Retain every valid endpoint. Remove self-follows and deduplicate directed (source, target) pairs. Self-loop-only users remain with zero degree."],
           ["Aggregate", "Count targets for followers and sources for followees; left-join both counts to the complete user inventory and fill missing degrees with zero."]], [.22, .78])
    add("Distributed degree aggregation", heading)
    add_figure(ASSETS / "spark-compute-source.png",
               f"Actual PySpark implementation, lines {core_start}-{functions['compute'].end_lineno}. Deduplication, joins and counts execute in Spark; complete selectable source appears in Appendix A.", 380)
    add(f"Conservation check: both degree sums equal {summary['unique_edges']:,} unique edges. Only compact audit totals are collected on the driver; edge and user DataFrames remain distributed.", small)

    page_title("Part 2.3 / Cloud evidence", "Completed job & S3 output")
    if matching_job:
        add(f"The submitted results were computed on Amazon EMR in {snapshot['provenance']['region']}. The Spark step completed on {completed}. The screenshots below show that completed run and its output.", small)
    else:
        add("Matching completed cloud execution is pending.", small)
    for name, text in [
        ("aws-emr-success.png", "EMR Steps console: the Spark step is Completed. The run name and Step ID identify this execution."),
        ("aws-s3-output.png", "S3 output for the same run. The users directory contains _SUCCESS and partitioned JSON; the console lists five objects.")]:
        if manual_capture_matches(name, validated):
            add_figure(ASSETS / name, text, 310)
        else:
            add("Pending reviewed screenshot: " + name, small)

    page_title("Part 2.3 / Cloud evidence", "Cluster & worker execution")
    if manual_capture_matches("aws-emr-cluster.png", validated):
        add_figure(ASSETS / "aws-emr-cluster.png",
                   "EMR cluster summary: one primary, one core worker and one task worker. Terminated is the expected state after the completed step.", 310)
    else:
        add("Pending reviewed screenshot: aws-emr-cluster.png", small)
    if matching_job:
        table([["Execution field", "Recorded value"],
               ["Spark step", f"{job['step']['Id']}  /  COMPLETED"],
               ["EMR cluster", job['cluster']['Id']],
               ["Runtime / region", f"EMR 7.5.0 / Spark 3.5.2 / {snapshot['provenance']['region']}"]], [.27, .73])
    add("Completed tasks on distinct workers", heading)
    if execution:
        add("Counters were read from this application's Spark monitoring API after computation. Driver records and executors without completed tasks are excluded.", small)
        add("Spark application: " + execution["application_id"], small)
        table([["Executor", "Worker host", "Tasks", "Cores"],
               *[[row['executor_id'], row['host'], row['completed_tasks'], row['total_cores']]
                 for row in execution['executors']]], [.15, .59, .13, .13], numeric=(2, 3))
        add(f"{len(execution['worker_hosts'])} distinct worker hosts completed tasks. Each executor uses 8 GiB; the primary, core and task instances are m5.xlarge.", small)
    else:
        add("Verified execution on two worker hosts is pending.", small)
    if manual_capture_matches("aws-spark-executors.png", validated):
        add_figure(ASSETS / "aws-spark-executors.png", "Optional Spark Executors console evidence for the same application.", 230)

    page_title("Validation / Reproducibility / References", "Validation & references")
    add("Independent verification", heading)
    verification = [["Check", "Recorded outcome"]]
    verification.append(["Full-dataset comparison",
                         f"PASS - all {local['compared_users']:,} users matched independent SQLite aggregation; local Spark was an additional verification run."
                         if local_valid else "PENDING - current independent verification is missing."])
    verification.append(["HTTPS browser checks",
                         f"PASS - {len(browser['checks'])} checks covered required counts, pagination, sorting, search, CSV, mobile layout and error recovery."
                         if browser_valid else "PENDING - current browser verification is missing."])
    verification.append(["Python / infrastructure",
                         f"PASS - {checks['python_tests']} Python tests; syntax checks and source/generated CloudFormation cfn-lint."
                         if checks_valid else "PENDING - current project checks are missing."])
    verification.append(["Spark boundary cases",
                         f"PASS - {len(edge['cases'])} cases covered dirty/empty input, loop-only users, CRLF, 20-digit IDs and invalid UTF-8/Unicode records."
                         if edge_valid else "PENDING - current Spark boundary checks are missing."])
    table(verification, [.29, .71])
    add("Reproducibility and limits", heading)
    add("Input and script keys are content-addressed; every Spark run uses a new output directory. The controller checks the completed step, success markers, two-worker evidence and independent result before publication. Failed or interrupted runs preserve the previous published result.", small)
    add("The API indexes the per-user snapshot in a 512 MB Lambda function and caches it for 30 seconds. This suits the supplied 81,306-user dataset; larger result sets require a partitioned query store. Spark's graph processing remains distributed.", small)
    add(f"Input: {summary['input_bytes']:,} bytes. SHA-256:", small)
    add(summary['input_sha256'], small)
    if browser.get('url'):
        website = html.escape(browser['url'], quote=True)
        story.append(Paragraph(f'Website: <link href="{website}" color="#006D77">{website}</link>', small))
        markdown.append(f"Website: [{browser['url']}]({browser['url']})")
    add("References", heading)
    reference_style = ParagraphStyle("ReferencesLab", parent=small, fontSize=8, leading=10.5, spaceAfter=6)
    for index, reference in enumerate(content['sources'], 1):
        title = html.escape(reference['title'])
        url = html.escape(reference['url'], quote=True)
        story.append(Paragraph(f'[{index}] {title}<br/><link href="{url}" color="#006D77">{url}</link>', reference_style))
        markdown.append(f"{index}. [{reference['title']}]({reference['url']})")
    add("The SNAP reference describes the source collection. Reported results use the modified lab input identified by the checksum above.", reference_style)

    # Divide the unchanged source at meaningful processing boundaries, keeping every line.
    aggregation_start = next(i for i, line in enumerate(source_lines, 1) if line.startswith('    incoming ='))
    helper_start = functions['write_execution_evidence'].lineno
    source_ranges = [(1, aggregation_start - 1, "Parsing & graph cleaning"),
                     (aggregation_start, helper_start - 1, "Aggregation & worker evidence"),
                     (helper_start, len(source_lines), "Output evidence & job entry point")]
    appendix_coverage = []
    for index, (start, end, title) in enumerate(source_ranges, 1):
        page_title(f"Appendix A / Complete PySpark source / {index} of {len(source_ranges)}", f"A.{index}  {title}")
        add(f"analysis/spark_job.py  |  Lines {start}-{end} of {len(source_lines)}. Line numbers refer to the implemented file; continuation marks indicate visual wrapping.", small)
        story.append(SourceListing(source, start, end, f"analysis/spark_job.py   /   {start}-{end}"))
        appendix_coverage.extend(range(start, end + 1))
        markdown.append("```python\n" + "\n".join(source_lines[start-1:end]) + "\n```")
    if appendix_coverage != list(range(1, len(source_lines) + 1)):
        raise ValueError("The source appendix must contain every source line exactly once")

    class ReportTemplate(SimpleDocTemplate):
        def afterFlowable(self, flowable):
            title = getattr(flowable, 'report_section', None)
            if title:
                key = f"section-{len(sections) + 1}"
                self.canv.bookmarkPage(key)
                self.canv.addOutlineEntry(title, key, level=0)
                sections.append({'page': self.page, 'title': title})

    def decorate(canvas, document):
        canvas.saveState()
        canvas.setFillColor(ACCENT)
        canvas.rect(MARGIN, PAGE_HEIGHT - 31, 23, 2, fill=1, stroke=0)
        canvas.setFont("Helvetica-Bold", 7)
        canvas.drawString(MARGIN + 30, PAGE_HEIGHT - 33, "CS5224  /  LAB 02")
        canvas.setFillColor(GRAY)
        canvas.setFont("Helvetica", 7)
        canvas.drawRightString(PAGE_WIDTH - MARGIN, PAGE_HEIGHT - 33, "CLOUD SERVICES & DATA ANALYTICS")
        canvas.setStrokeColor(LINE)
        canvas.line(MARGIN, 39, PAGE_WIDTH - MARGIN, 39)
        canvas.setFont("Helvetica", 7.5)
        canvas.drawString(MARGIN, 25, f"{student_name}  /  {student_id}")
        canvas.drawRightString(PAGE_WIDTH - MARGIN, 25, f"{document.page:02d}")
        if draft:
            canvas.setFont("Helvetica-Bold", 7)
            canvas.setFillColor(ACCENT)
            canvas.drawCentredString(PAGE_WIDTH / 2, 25, "DRAFT - CLOUD EVIDENCE PENDING")
        canvas.restoreState()

    destination = report / f"{student_id}.pdf"
    document = ReportTemplate(str(destination), pagesize=(PAGE_WIDTH, PAGE_HEIGHT),
                              topMargin=52, bottomMargin=52, leftMargin=MARGIN, rightMargin=MARGIN,
                              title="CS5224 Lab 2 - Cloud Services and Twitter Analytics",
                              author=student_name,
                              subject=f"{student_id}; {'draft' if draft else 'cloud evidence included'}")
    document.build(story, onFirstPage=decorate, onLaterPages=decorate)
    markdown_path = report / f"{student_id}.md"
    markdown_path.write_text("# CS5224 Lab 2 - Cloud Services & Twitter Analytics\n\n" + "\n\n".join(markdown) + "\n", encoding="utf-8")
    (ROOT / "artifacts/report-status.json").write_text(json.dumps({
        "student_name": student_name, "student_id": student_id,
        "status": "DRAFT_CLOUD_EVIDENCE_PENDING" if draft else "CLOUD_EVIDENCE_INCLUDED",
        "missing_evidence": missing, "frontend_words": len(content['frontend_setup'].split()),
        "backend_words": len(content['backend_setup'].split()), "snapshot_id": validated.snapshot_id,
        "local_verification_current": local_valid, "browser_verification_current": browser_valid,
        "report_images_current": captures_valid, "project_checks_current": checks_valid,
        "spark_edge_verification_current": edge_valid, "pages": document.page,
        "sections": sections, "appendix_source_lines": len(appendix_coverage)
    }, indent=2) + "\n")
    print(destination)
    print(markdown_path)
    print("Pages:", document.page)
    print("Status:", "DRAFT; real cloud evidence required" if draft else "Cloud evidence included")


if __name__ == "__main__":
    main()
