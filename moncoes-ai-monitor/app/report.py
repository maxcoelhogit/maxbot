from __future__ import annotations

import argparse
import os
import shutil
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

import requests
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from common import (
    CONFIG_DIR,
    REPORT_DIR,
    TMP_DIR,
    capture_playback_frames,
    db,
    load_json,
    make_contact_sheet,
    now_local,
    safe_remove,
)

NAVY = colors.HexColor("#193A56")
BLUE = colors.HexColor("#315F83")
SLATE = colors.HexColor("#65727C")
PALE_BLUE = colors.HexColor("#EAF1F6")
PALE_GREEN = colors.HexColor("#E7F2EB")
PALE_YELLOW = colors.HexColor("#FFF1C9")
PALE_RED = colors.HexColor("#F8E3E3")
BORDER = colors.HexColor("#CAD6DF")
TEXT = colors.HexColor("#26343D")

STATUS_LABEL = {
    "normal": "ROTINA",
    "potential_occurrence": "POTENCIAL OCORRÊNCIA",
    "critical": "CRÍTICO",
    "uncertain": "REQUER CONTEXTO",
}
STATUS_BG = {
    "normal": PALE_GREEN,
    "potential_occurrence": PALE_YELLOW,
    "critical": PALE_RED,
    "uncertain": PALE_YELLOW,
}


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _fmt(value: str | None) -> str:
    dt = _dt(value)
    if not dt:
        return "-"
    return dt.strftime("%d/%m/%Y %H:%M")


def _status_label(value: str | None) -> str:
    return STATUS_LABEL.get(value or "", "EVENTO")


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(BORDER)
    canvas.setLineWidth(0.4)
    canvas.line(1.5 * cm, 1.05 * cm, A4[0] - 1.5 * cm, 1.05 * cm)
    canvas.setFillColor(SLATE)
    canvas.setFont("Helvetica", 7.5)
    canvas.drawString(
        1.5 * cm,
        0.62 * cm,
        "Condomínio Edifício Monções - Monitoramento por IA - uso interno",
    )
    canvas.drawRightString(
        A4[0] - 1.5 * cm,
        0.62 * cm,
        f"Página {doc.page}",
    )
    canvas.restoreState()


def _styles():
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="ReportTitle",
            parent=styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=22,
            leading=25,
            textColor=NAVY,
            alignment=TA_LEFT,
            spaceAfter=8,
        )
    )
    styles.add(
        ParagraphStyle(
            name="ReportSubtitle",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=11,
            leading=15,
            textColor=SLATE,
            spaceAfter=12,
        )
    )
    styles.add(
        ParagraphStyle(
            name="Section",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=16,
            leading=19,
            textColor=NAVY,
            spaceBefore=8,
            spaceAfter=8,
        )
    )
    styles.add(
        ParagraphStyle(
            name="Subsection",
            parent=styles["Heading3"],
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=15,
            textColor=BLUE,
            spaceBefore=6,
            spaceAfter=5,
        )
    )
    styles.add(
        ParagraphStyle(
            name="BodyReport",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=9.4,
            leading=13,
            textColor=TEXT,
            spaceAfter=5,
        )
    )
    styles.add(
        ParagraphStyle(
            name="Small",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=8,
            leading=10.5,
            textColor=SLATE,
        )
    )
    styles.add(
        ParagraphStyle(
            name="Badge",
            parent=styles["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=8.3,
            leading=10,
            textColor=SLATE,
            alignment=TA_CENTER,
        )
    )
    styles.add(
        ParagraphStyle(
            name="MetricValue",
            parent=styles["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=19,
            leading=21,
            textColor=SLATE,
            alignment=TA_CENTER,
        )
    )
    styles.add(
        ParagraphStyle(
            name="MetricLabel",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=7.8,
            leading=10,
            textColor=SLATE,
            alignment=TA_CENTER,
        )
    )
    return styles


def _metric(value: int, label: str, styles):
    return Table(
        [[Paragraph(str(value), styles["MetricValue"])],
         [Paragraph(escape(label), styles["MetricLabel"])]],
        colWidths=[4.0 * cm],
        rowHeights=[1.05 * cm, 0.75 * cm],
        style=TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PALE_BLUE),
                ("BOX", (0, 0), (-1, -1), 0.4, BORDER),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ]
        ),
    )


def _representative_candidates(rows, target: int):
    """Return enough normal-event candidates to reliably obtain report images.

    Prefer historical poll events because playback is usually the most stable,
    but keep listener/realtime events as fallbacks. We return several
    candidates instead of exactly the requested count because a single DVR
    playback window can occasionally be unavailable.
    """
    normals = [
        r for r in rows
        if r["final_status"] == "normal"
        and not r["error"]
    ]

    source_rank = {"poll": 0, "listener_stop": 1, "realtime": 2}
    normals = sorted(
        normals,
        key=lambda r: (
            source_rank.get(r["source"], 9),
            -( _dt(r["started_at"]).timestamp() if _dt(r["started_at"]) else 0 ),
        ),
    )

    # First offer different cameras; then allow additional events from the
    # same camera so one bad playback window does not remove all photos.
    ordered = []
    used = set()
    for row in normals:
        key = (row["dvr"], row["channel"])
        if key not in used:
            ordered.append(row)
            used.add(key)
    ordered.extend(row for row in normals if row not in ordered)

    return ordered[: max(8, target * 8)]


def _event_image(row, cameras, temp_root: Path) -> Path | None:
    evidence = row["evidence_path"]
    if evidence and Path(evidence).is_file():
        return Path(evidence)

    start = _dt(row["started_at"])
    if not start:
        return None
    end = _dt(row["ended_at"]) or (start + timedelta(seconds=20))
    dvr = str(row["dvr"])
    channel = int(row["channel"])

    cam = cameras.get(dvr, {}).get("channels", {}).get(str(channel), {})
    host = cameras.get(dvr, {}).get("host")
    if not host:
        return None

    user = os.getenv("DVR_USER", "admin")
    password = os.getenv("DVR_PASS", "")
    if not password:
        return None

    work = temp_root / f"{dvr}_{channel}_{row['id']}"
    sheet = work / "contact.jpg"
    try:
        frames = capture_playback_frames(
            host,
            user,
            password,
            channel,
            start,
            end,
            work,
            count=4,
            rotate=int(cam.get("rotate", 0)),
        )
        make_contact_sheet(frames, sheet)
        return sheet
    except Exception:
        safe_remove(work)
        return None


def _camera_summary(rows, cameras, styles):
    data = [[
        Paragraph("<b>Câmera</b>", styles["Small"]),
        Paragraph("<b>Local</b>", styles["Small"]),
        Paragraph("<b>Processados</b>", styles["Small"]),
        Paragraph("<b>Revisão</b>", styles["Small"]),
        Paragraph("<b>Críticos</b>", styles["Small"]),
    ]]
    for dvr, dvr_cfg in cameras.items():
        for channel_text, cam in dvr_cfg.get("channels", {}).items():
            if not cam.get("enabled", False):
                continue
            channel = int(channel_text)
            subset = [
                r for r in rows
                if str(r["dvr"]) == str(dvr) and int(r["channel"]) == channel
            ]
            review = sum(
                1 for r in subset
                if r["final_status"] in ("potential_occurrence", "uncertain")
            )
            critical = sum(1 for r in subset if r["final_status"] == "critical")
            data.append([
                Paragraph(f"{escape(str(dvr))}/{channel}", styles["Small"]),
                Paragraph(escape(cam.get("name", f"Câmera {channel}")), styles["Small"]),
                Paragraph(str(len(subset)), styles["Small"]),
                Paragraph(str(review), styles["Small"]),
                Paragraph(str(critical), styles["Small"]),
            ])

    table = Table(
        data,
        colWidths=[1.55 * cm, 8.35 * cm, 2.2 * cm, 2.0 * cm, 1.8 * cm],
        repeatRows=1,
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.35, BORDER),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table


def _event_block(row, image_path: Path | None, styles):
    status = row["final_status"] or "uncertain"
    badge = Table(
        [[Paragraph(_status_label(status), styles["Badge"])]],
        colWidths=[4.2 * cm],
        rowHeights=[0.65 * cm],
        style=TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), STATUS_BG.get(status, PALE_BLUE)),
                ("BOX", (0, 0), (-1, -1), 0.35, BORDER),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        ),
    )
    heading = Table(
        [[
            Paragraph(
                f"<b>{escape(_fmt(row['started_at']))} - "
                f"{escape(row['camera_name'] or ('Câmera ' + str(row['channel'])))}</b>",
                styles["Subsection"],
            ),
            badge,
        ]],
        colWidths=[11.8 * cm, 4.2 * cm],
        style=TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        ),
    )

    body = [
        heading,
        Paragraph(
            escape(row["description"] or row["category"] or "Evento monitorado."),
            styles["BodyReport"],
        ),
    ]
    if row["rule_reference"]:
        body.append(
            Paragraph(
                f"<b>Regra relacionada:</b> {escape(row['rule_reference'])}",
                styles["Small"],
            )
        )
    conf = row["confidence"]
    if conf is not None:
        body.append(
            Paragraph(
                f"Confiança da classificação: {float(conf):.0%}. "
                "Classificação automática sujeita a revisão humana.",
                styles["Small"],
            )
        )
    if image_path and image_path.is_file():
        body.extend(
            [
                Spacer(1, 4),
                Image(
                    str(image_path),
                    width=16.0 * cm,
                    height=9.0 * cm,
                    kind="proportional",
                ),
            ]
        )
    body.append(Spacer(1, 9))
    return KeepTogether(body)


def build(period: str) -> Path:
    now = now_local()
    start = now - timedelta(days=1 if period == "daily" else 7)
    cameras = load_json(CONFIG_DIR / "cameras.json")

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
    errors = [row for row in rows if row["error"]]
    relevant = [
        row
        for row in rows
        if row["final_status"]
        in ("potential_occurrence", "critical", "uncertain")
    ]

    # Operational reports should be concise. Show every critical event first,
    # then the most recent review-needed events, with a reasonable cap.
    max_highlights = 6 if period == "weekly" else 4
    critical = [r for r in relevant if r["final_status"] == "critical"]
    other_relevant = [
        r for r in relevant if r["final_status"] != "critical"
    ]
    highlights = (critical + list(reversed(other_relevant)))[:max_highlights]
    highlights.sort(key=lambda r: r["started_at"] or "")

    representative_target = 2 if period == "weekly" else 1

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    output = REPORT_DIR / f"relatorio_{period}_{now.strftime('%Y%m%d_%H%M%S')}.pdf"
    report_tmp = TMP_DIR / f"report_{period}_{now.strftime('%Y%m%d_%H%M%S')}"
    report_tmp.mkdir(parents=True, exist_ok=True)

    representative_assets = []
    for row in _representative_candidates(rows, representative_target):
        image_path = _event_image(row, cameras, report_tmp)
        if not image_path:
            continue
        representative_assets.append((row, image_path))
        if len(representative_assets) >= representative_target:
            break

    doc = SimpleDocTemplate(
        str(output),
        pagesize=A4,
        rightMargin=1.45 * cm,
        leftMargin=1.45 * cm,
        topMargin=1.45 * cm,
        bottomMargin=1.35 * cm,
    )
    styles = _styles()
    title = "DIÁRIO" if period == "daily" else "SEMANAL"

    story = [
        Spacer(1, 0.35 * cm),
        Paragraph(f"RELATÓRIO {title} DE MONITORAMENTO", styles["ReportTitle"]),
        Paragraph(
            "Monitoramento por IA - Condomínio Edifício Monções",
            styles["ReportSubtitle"],
        ),
        Table(
            [[
                Paragraph(
                    f"<b>Período analisado</b><br/>{start.strftime('%d/%m/%Y %H:%M')} "
                    f"a {now.strftime('%d/%m/%Y %H:%M')}",
                    styles["BodyReport"],
                ),
                Paragraph(
                    f"<b>Modo</b><br/>{escape(os.getenv('MONCOES_MODE', 'observe'))}",
                    styles["BodyReport"],
                ),
            ]],
            colWidths=[11.0 * cm, 5.0 * cm],
            style=TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), PALE_BLUE),
                    ("BOX", (0, 0), (-1, -1), 0.4, BORDER),
                    ("INNERGRID", (0, 0), (-1, -1), 0.35, BORDER),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ]
            ),
        ),
        Spacer(1, 12),
        Paragraph("Resumo executivo", styles["Section"]),
        Table(
            [[
                _metric(len(rows), "eventos processados", styles),
                _metric(
                    counts["potential_occurrence"] + counts["uncertain"],
                    "para revisão",
                    styles,
                ),
                _metric(counts["critical"], "críticos", styles),
                _metric(len(errors), "erros técnicos", styles),
            ]],
            colWidths=[4.0 * cm] * 4,
            style=TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 2),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ]
            ),
        ),
        Spacer(1, 10),
        Paragraph(
            (
                "O relatório prioriza exceções e contexto operacional. "
                "Movimentos cotidianos classificados como rotina são resumidos; "
                "situações potenciais ou críticas são destacadas para revisão. "
                "O sistema não emite advertência, multa ou decisão disciplinar automaticamente."
            ),
            styles["BodyReport"],
        ),
    ]

    if counts["critical"]:
        conclusion_bg = PALE_RED
        conclusion = (
            f"Foram detectados {counts['critical']} evento(s) crítico(s) no período. "
            "Consulte os destaques abaixo e os reconhecimentos no Monções Alertas."
        )
    elif counts["potential_occurrence"] + counts["uncertain"]:
        conclusion_bg = PALE_YELLOW
        conclusion = (
            f"Há {counts['potential_occurrence'] + counts['uncertain']} evento(s) "
            "que requerem revisão humana ou contexto adicional."
        )
    else:
        conclusion_bg = PALE_GREEN
        conclusion = (
            "Nenhuma ocorrência potencial ou crítica foi destacada entre os "
            "eventos processados no período."
        )

    story.extend(
        [
            Table(
                [[Paragraph(f"<b>Leitura do período:</b> {escape(conclusion)}", styles["BodyReport"])]],
                colWidths=[16.0 * cm],
                style=TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), conclusion_bg),
                        ("BOX", (0, 0), (-1, -1), 0.4, BORDER),
                        ("LEFTPADDING", (0, 0), (-1, -1), 8),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                        ("TOPPADDING", (0, 0), (-1, -1), 8),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                    ]
                ),
            ),
            Spacer(1, 12),
            Paragraph("Cobertura por câmera", styles["Section"]),
            _camera_summary(rows, cameras, styles),
        ]
    )

    if highlights:
        story.extend([PageBreak(), Paragraph("Ocorrências para revisão", styles["Section"])])
        for row in highlights:
            image_path = _event_image(row, cameras, report_tmp)
            story.append(_event_block(row, image_path, styles))
        if len(relevant) > len(highlights):
            story.append(
                Paragraph(
                    f"Outros {len(relevant) - len(highlights)} evento(s) para revisão "
                    "permanecem disponíveis no histórico do Monções Alertas.",
                    styles["Small"],
                )
            )
    else:
        story.extend(
            [
                Spacer(1, 10),
                Paragraph("Ocorrências para revisão", styles["Section"]),
                Paragraph(
                    "Nenhuma ocorrência potencial, incerta ou crítica foi destacada no período.",
                    styles["BodyReport"],
                ),
            ]
        )

    if representative_assets:
        story.extend([PageBreak(), Paragraph("Exemplos representativos de rotina", styles["Section"])])
        story.append(
            Paragraph(
                "As imagens abaixo ilustram eventos processados e descartados pela triagem "
                "como rotina. Elas ajudam a verificar visualmente o comportamento do filtro "
                "sem transformar o relatório em uma listagem de todos os movimentos.",
                styles["BodyReport"],
            )
        )
        for row, image_path in representative_assets:
            story.append(_event_block(row, image_path, styles))

    story.extend(
        [
            Spacer(1, 10),
            Paragraph("Situação técnica", styles["Section"]),
        ]
    )
    if errors:
        err_data = [[
            Paragraph("<b>Data/hora</b>", styles["Small"]),
            Paragraph("<b>Câmera</b>", styles["Small"]),
            Paragraph("<b>Falha</b>", styles["Small"]),
        ]]
        for row in errors[-8:]:
            message = (row["error"] or "").replace("\n", " ")
            if len(message) > 180:
                message = message[:177] + "..."
            err_data.append([
                Paragraph(escape(_fmt(row["started_at"])), styles["Small"]),
                Paragraph(escape(row["camera_name"] or "-"), styles["Small"]),
                Paragraph(escape(message), styles["Small"]),
            ])
        err_table = Table(
            err_data,
            colWidths=[3.2 * cm, 4.5 * cm, 8.3 * cm],
            repeatRows=1,
            style=TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.35, BORDER),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            ),
        )
        story.append(err_table)
    else:
        story.append(
            Paragraph(
                "Nenhum erro técnico foi registrado nos eventos incluídos neste período.",
                styles["BodyReport"],
            )
        )

    story.extend(
        [
            Spacer(1, 10),
            Table(
                [[Paragraph(
                    "<b>Nota de uso:</b> este documento é uma ferramenta de triagem e apoio "
                    "à gestão. Qualquer medida administrativa depende de revisão humana, "
                    "confirmação do contexto e aplicação das regras do condomínio.",
                    styles["BodyReport"],
                )]],
                colWidths=[16.0 * cm],
                style=TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), PALE_BLUE),
                        ("BOX", (0, 0), (-1, -1), 0.4, BORDER),
                        ("LEFTPADDING", (0, 0), (-1, -1), 8),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                        ("TOPPADDING", (0, 0), (-1, -1), 8),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                    ]
                ),
            ),
        ]
    )

    try:
        doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    finally:
        safe_remove(report_tmp)

    portal = os.getenv("PORTAL_BASE_URL", "").strip().rstrip("/")
    token = os.getenv("PORTAL_INGEST_TOKEN", "").strip()
    if portal and token:
        try:
            with output.open("rb") as fh:
                requests.post(
                    f"{portal}/api/ingest/report",
                    params={"kind": period},
                    files={"file": (output.name, fh, "application/pdf")},
                    headers={"X-Ingest-Token": token},
                    timeout=30,
                ).raise_for_status()
        except Exception:
            pass

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
