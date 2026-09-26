from __future__ import annotations

import argparse
from datetime import timedelta
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from common import REPORT_DIR, db, now_local


def build(period: str) -> Path:
    now = now_local()
    start = now - timedelta(days=1 if period == "daily" else 7)

    with db() as con:
        rows = con.execute(
            "SELECT * FROM events WHERE started_at>=? AND started_at<? ORDER BY started_at",
            (start.isoformat(), now.isoformat()),
        ).fetchall()

    statuses = ["normal", "potential_occurrence", "critical", "uncertain"]
    counts = {
        status: sum(1 for row in rows if row["final_status"] == status)
        for status in statuses
    }
    errors = sum(1 for row in rows if row["error"])
    relevant = [
        row
        for row in rows
        if row["final_status"]
        in ("potential_occurrence", "critical", "uncertain")
    ]

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    output = (
        REPORT_DIR
        / f"relatorio_{period}_{now.strftime('%Y%m%d_%H%M%S')}.pdf"
    )

    doc = SimpleDocTemplate(
        str(output),
        pagesize=A4,
        rightMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        topMargin=1.4 * cm,
        bottomMargin=1.4 * cm,
    )
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="CenterTitle",
            parent=styles["Title"],
            alignment=TA_CENTER,
            spaceAfter=12,
        )
    )

    title = "diário" if period == "daily" else "semanal"
    story = [
        Paragraph("Condomínio Edifício Monções", styles["CenterTitle"]),
        Paragraph(f"Relatório {title} de triagem por IA", styles["Heading2"]),
        Paragraph(
            f"Período: {start.strftime('%d/%m/%Y %H:%M')} "
            f"a {now.strftime('%d/%m/%Y %H:%M')}",
            styles["BodyText"],
        ),
        Spacer(1, 10),
    ]

    summary = [
        ["Eventos processados", str(len(rows))],
        ["Rotina", str(counts["normal"])],
        ["Potenciais ocorrências", str(counts["potential_occurrence"])],
        ["Críticos", str(counts["critical"])],
        ["Requer contexto", str(counts["uncertain"])],
        ["Erros técnicos", str(errors)],
    ]
    table = Table(summary, colWidths=[8 * cm, 4 * cm])
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story += [
        table,
        Spacer(1, 12),
        Paragraph(
            "O sistema realiza triagem conservadora. Nenhum resultado "
            "constitui advertência, multa ou decisão disciplinar; toda "
            "ocorrência potencial requer revisão humana.",
            styles["BodyText"],
        ),
        Spacer(1, 12),
    ]

    if not relevant:
        story.append(
            Paragraph(
                "Nenhuma ocorrência potencial com evidência suficiente "
                "foi destacada no período.",
                styles["Heading3"],
            )
        )
    else:
        story.append(Paragraph("Eventos destacados", styles["Heading2"]))
        for index, row in enumerate(relevant, 1):
            started = (row["started_at"] or "")[:19].replace("T", " ")
            story += [
                Paragraph(
                    f"{index}. {row['camera_name']} — {started}",
                    styles["Heading3"],
                ),
                Paragraph(
                    f"Classificação: <b>{row['final_status']}</b> | "
                    f"Confiança: {(row['confidence'] or 0):.2f}",
                    styles["BodyText"],
                ),
                Paragraph(
                    f"Descrição: {row['description'] or ''}",
                    styles["BodyText"],
                ),
                Paragraph(
                    f"Referência: {row['rule_reference'] or 'nenhuma'}",
                    styles["BodyText"],
                ),
            ]

            evidence = row["evidence_path"]
            if evidence and Path(evidence).exists():
                image = Image(
                    evidence,
                    width=16.5 * cm,
                    height=9.5 * cm,
                    kind="proportional",
                )
                story += [Spacer(1, 6), image]
            story += [Spacer(1, 12)]

    doc.build(story)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--period",
        choices=["daily", "weekly"],
        default="daily",
    )
    args = parser.parse_args()
    print(build(args.period))
