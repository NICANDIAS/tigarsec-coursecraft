"""
Rebuilds the made-up test specs in tools/fixtures/ (only needed if they ever have to change).
Needs reportlab:  pip install reportlab   then:
  python tools/make_layout_fixtures.py tools/fixtures/layout_edexcel_like.pdf
  python tools/make_layout_fixtures.py tools/fixtures/layout_aqa_like.pdf
  python tools/make_layout_fixtures.py tools/fixtures/layout_plain_header.pdf
"""
import sys
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak

out = sys.argv[1]
ss = getSampleStyleSheet()
body = ParagraphStyle("b", parent=ss["Normal"], fontName="Helvetica", fontSize=10, leading=12.5)
h1 = ParagraphStyle("h1", parent=ss["Heading1"], fontName="Helvetica-Bold", fontSize=16, spaceAfter=10)
h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=12, spaceAfter=6)

def cell(lines):
    # one Paragraph per statement, with a blank gap between statements (like real specs)
    return [Paragraph(l, body) if i == 0 else Paragraph("<br/>" + l, body) for i, l in enumerate(lines)]

def footer(c, d):
    c.setFont("Helvetica", 8); c.drawString(50, 30, f"Sample Board Level 3 Specimen - Issue 1 - page {d.page}")

VERBS = ["Explain", "Describe", "Analyse", "Evaluate", "Identify", "Calculate", "Discuss", "Compare"]
def statements(topic, n, long_every=3):
    out = []
    for k in range(n):
        s = f"{VERBS[k % len(VERBS)]} the {topic.lower()} idea number {k + 1} and its effect on organisations"
        if k % long_every == 1:
            s += ", including the reasons why managers and other stakeholders might respond in different ways over time"
        out.append(s)
    return out

story = []
if "edexcel" in out:
    # Layout 1: "Topic N: title" headings; 3-column tables Code | Subject content | What students need to learn
    story += [Paragraph("Specimen Level 3 Advanced GCE in Enterprise", h1), Paragraph("Contents", h2),
              Paragraph("Topic 1: Running a business 4", body), Paragraph("Topic 2: Money and growth 6", body), PageBreak()]
    plan = [("Topic 1: Running a business", [("1.1", "Purpose of enterprise", 4), ("1.2", "Owners and their aims", 9),
                                            ("1.3", "Planning a start-up", 5)]),
            ("Topic 2: Money and growth", [("2.1", "Sources of money", 6), ("2.2", "Cash and profit", 12), ("2.3", "Growing a firm", 3)])]
    for title, rows in plan:
        story += [Paragraph(title, h1), Paragraph("Students will study how firms start and grow.", body), Spacer(1, 8)]
        data = [["Code", "Subject content", "What students need to learn"]]
        for code, topic, n in rows:
            data.append([Paragraph(code, body), Paragraph(topic, body), cell(statements(topic, n))])
        t = Table(data, colWidths=[45, 120, 330], repeatRows=1)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey)]))
        story += [t, PageBreak()]
elif "plain" in out:
    # Layout 3: header row NOT bold (so it can't be found from the page alone - needs the AI's description);
    # "Unit N:" headings; 2 columns Topic | Learners should be able to
    story += [Paragraph("Specimen Level 3 Certificate in Retail", h1), PageBreak()]
    plain = ParagraphStyle("p", parent=body)
    for u, (title, rows) in enumerate([("Unit 1: Customer service", [("Greeting customers", 5), ("Handling complaints", 7)]),
                                       ("Unit 2: Stock control", [("Receiving deliveries", 6), ("Counting stock", 4), ("Reordering", 5)])], 1):
        story += [Paragraph(title, h1)]
        data = [[Paragraph("Topic", plain), Paragraph("Learners should be able to", plain)]]
        for topic, n in rows:
            data.append([Paragraph(topic, body), cell(statements(topic, n))])
        t = Table(data, colWidths=[140, 355], repeatRows=1)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story += [t, PageBreak()]
else:
    # Layout 2: numbered section headings 3.1 (unit) and 3.1.1 (topic); tables Content | Additional information
    story += [Paragraph("Specimen AS and A-level Commerce", h1), Paragraph("3 Subject content", h1), PageBreak()]
    plan = [("3.1 Markets and customers", [("3.1.1 Understanding markets", 5), ("3.1.2 Customer needs", 8)]),
            ("3.2 People at work", [("3.2.1 Leading people", 6), ("3.2.2 Rewarding staff", 4), ("3.2.3 Team working", 7)])]
    for title, topics in plan:
        story += [Paragraph(title, h1)]
        for ttitle, n in topics:
            story += [Paragraph(ttitle, h2)]
            data = [["Content", "Additional information"]]
            sts = statements(ttitle.split(" ", 1)[1], n)
            for k, s in enumerate(sts):
                data.append([Paragraph(s, body), Paragraph(f"Learners should consider examples {k + 1} from real firms." if k % 2 == 0 else "", body)])
            t = Table(data, colWidths=[300, 195], repeatRows=1)
            t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")]))
            story += [t, Spacer(1, 10)]
        story += [PageBreak()]
SimpleDocTemplate(out, pagesize=A4, leftMargin=50, rightMargin=50, topMargin=50, bottomMargin=50).build(story, onFirstPage=footer, onLaterPages=footer)
print("built", out)
