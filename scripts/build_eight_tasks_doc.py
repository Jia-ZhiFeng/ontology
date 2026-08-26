import json
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "个人信审本体八项任务总结.docx"
METRICS = json.loads((ROOT / "personal_credit_release_metrics.json").read_text(encoding="utf-8"))
MODEL_STATS = METRICS["model"]
DATABASE_STATS = METRICS["database"]
AUDIT_STATS = METRICS["audit"]
GRAPH_STATS = METRICS["graph"]

BLUE = "2E74B5"
DARK_BLUE = "1F4D78"
NAVY = "0B2545"
INK = "1F2937"
GRAY = "667085"
LIGHT_GRAY = "F2F4F7"
BLUE_GRAY = "E8EEF5"
CALLOUT = "F4F6F9"
WHITE = "FFFFFF"
GREEN = "047857"
BORDER = "D0D5DD"


def set_run_font(run, *, size=11, bold=None, italic=None, color=INK, ascii_font="Calibri", east_asia="Microsoft YaHei"):
    run.font.name = ascii_font
    run._element.get_or_add_rPr().rFonts.set(qn("w:ascii"), ascii_font)
    run._element.get_or_add_rPr().rFonts.set(qn("w:hAnsi"), ascii_font)
    run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), east_asia)
    run.font.size = Pt(size)
    run.font.color.rgb = RGBColor.from_string(color)
    if bold is not None:
        run.bold = bold
    if italic is not None:
        run.italic = italic


def set_cell_margins(cell, top=80, start=120, bottom=80, end=120):
    tc = cell._tc
    tc_pr = tc.get_or_add_tcPr()
    tc_mar = tc_pr.first_child_found_in("w:tcMar")
    if tc_mar is None:
        tc_mar = OxmlElement("w:tcMar")
        tc_pr.append(tc_mar)
    for tag, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = tc_mar.find(qn(f"w:{tag}"))
        if node is None:
            node = OxmlElement(f"w:{tag}")
            tc_mar.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def shade_cell(cell, fill):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def set_cell_border(cell, color=BORDER, size="6"):
    tc_pr = cell._tc.get_or_add_tcPr()
    borders = tc_pr.first_child_found_in("w:tcBorders")
    if borders is None:
        borders = OxmlElement("w:tcBorders")
        tc_pr.append(borders)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        node = borders.find(qn(f"w:{edge}"))
        if node is None:
            node = OxmlElement(f"w:{edge}")
            borders.append(node)
        node.set(qn("w:val"), "single")
        node.set(qn("w:sz"), size)
        node.set(qn("w:color"), color)


def set_repeat_header(row):
    tr_pr = row._tr.get_or_add_trPr()
    tbl_header = OxmlElement("w:tblHeader")
    tbl_header.set(qn("w:val"), "true")
    tr_pr.append(tbl_header)


def set_table_geometry(table, widths_dxa):
    if sum(widths_dxa) != 9360:
        raise ValueError(f"table widths must total 9360 DXA, got {sum(widths_dxa)}")
    table.autofit = False
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    tbl_pr = table._tbl.tblPr
    tbl_w = tbl_pr.find(qn("w:tblW"))
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:w"), "9360")
    tbl_w.set(qn("w:type"), "dxa")
    tbl_ind = tbl_pr.find(qn("w:tblInd"))
    if tbl_ind is None:
        tbl_ind = OxmlElement("w:tblInd")
        tbl_pr.append(tbl_ind)
    tbl_ind.set(qn("w:w"), "120")
    tbl_ind.set(qn("w:type"), "dxa")
    layout = tbl_pr.find(qn("w:tblLayout"))
    if layout is None:
        layout = OxmlElement("w:tblLayout")
        tbl_pr.append(layout)
    layout.set(qn("w:type"), "fixed")
    grid = table._tbl.tblGrid
    for child in list(grid):
        grid.remove(child)
    for width in widths_dxa:
        col = OxmlElement("w:gridCol")
        col.set(qn("w:w"), str(width))
        grid.append(col)
    for row in table.rows:
        for idx, cell in enumerate(row.cells):
            tc_pr = cell._tc.get_or_add_tcPr()
            tc_w = tc_pr.find(qn("w:tcW"))
            if tc_w is None:
                tc_w = OxmlElement("w:tcW")
                tc_pr.append(tc_w)
            tc_w.set(qn("w:w"), str(widths_dxa[idx]))
            tc_w.set(qn("w:type"), "dxa")
            cell.width = Inches(widths_dxa[idx] / 1440)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            set_cell_margins(cell)
            set_cell_border(cell)


def set_paragraph_border(paragraph, *, side="bottom", color=BLUE, size="12", space="5"):
    p_pr = paragraph._p.get_or_add_pPr()
    p_bdr = p_pr.find(qn("w:pBdr"))
    if p_bdr is None:
        p_bdr = OxmlElement("w:pBdr")
        p_pr.append(p_bdr)
    border = OxmlElement(f"w:{side}")
    border.set(qn("w:val"), "single")
    border.set(qn("w:sz"), size)
    border.set(qn("w:space"), space)
    border.set(qn("w:color"), color)
    p_bdr.append(border)


def shade_paragraph(paragraph, fill=CALLOUT):
    p_pr = paragraph._p.get_or_add_pPr()
    shd = p_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        p_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def add_page_field(run):
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    run._r.extend([fld_begin, instr, fld_end])


def add_bullet_numbering(doc):
    numbering = doc.part.numbering_part.element
    existing_abs = [int(x.get(qn("w:abstractNumId"))) for x in numbering.findall(qn("w:abstractNum"))]
    existing_num = [int(x.get(qn("w:numId"))) for x in numbering.findall(qn("w:num"))]
    abstract_id = max(existing_abs or [0]) + 1
    num_id = max(existing_num or [0]) + 1

    abstract = OxmlElement("w:abstractNum")
    abstract.set(qn("w:abstractNumId"), str(abstract_id))
    multi = OxmlElement("w:multiLevelType")
    multi.set(qn("w:val"), "singleLevel")
    abstract.append(multi)
    lvl = OxmlElement("w:lvl")
    lvl.set(qn("w:ilvl"), "0")
    start = OxmlElement("w:start")
    start.set(qn("w:val"), "1")
    num_fmt = OxmlElement("w:numFmt")
    num_fmt.set(qn("w:val"), "bullet")
    lvl_text = OxmlElement("w:lvlText")
    lvl_text.set(qn("w:val"), "•")
    lvl_jc = OxmlElement("w:lvlJc")
    lvl_jc.set(qn("w:val"), "left")
    p_pr = OxmlElement("w:pPr")
    tabs = OxmlElement("w:tabs")
    tab = OxmlElement("w:tab")
    tab.set(qn("w:val"), "num")
    tab.set(qn("w:pos"), "720")
    tabs.append(tab)
    indent = OxmlElement("w:ind")
    indent.set(qn("w:left"), "720")
    indent.set(qn("w:hanging"), "360")
    spacing = OxmlElement("w:spacing")
    spacing.set(qn("w:after"), "160")
    spacing.set(qn("w:line"), "280")
    spacing.set(qn("w:lineRule"), "auto")
    p_pr.extend([tabs, indent, spacing])
    lvl.extend([start, num_fmt, lvl_text, lvl_jc, p_pr])
    abstract.append(lvl)
    numbering.append(abstract)

    num = OxmlElement("w:num")
    num.set(qn("w:numId"), str(num_id))
    abstract_ref = OxmlElement("w:abstractNumId")
    abstract_ref.set(qn("w:val"), str(abstract_id))
    num.append(abstract_ref)
    numbering.append(num)
    return num_id


def add_bullet(doc, text, num_id):
    paragraph = doc.add_paragraph()
    p_pr = paragraph._p.get_or_add_pPr()
    num_pr = OxmlElement("w:numPr")
    ilvl = OxmlElement("w:ilvl")
    ilvl.set(qn("w:val"), "0")
    num_id_el = OxmlElement("w:numId")
    num_id_el.set(qn("w:val"), str(num_id))
    num_pr.extend([ilvl, num_id_el])
    p_pr.append(num_pr)
    paragraph.paragraph_format.space_after = Pt(8)
    paragraph.paragraph_format.line_spacing = 1.167
    run = paragraph.add_run(text)
    set_run_font(run)
    return paragraph


def add_body(doc, text, *, bold_label=None, keep=False):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(6)
    p.paragraph_format.line_spacing = 1.10
    p.paragraph_format.keep_together = keep
    if bold_label and text.startswith(bold_label):
        r1 = p.add_run(bold_label)
        set_run_font(r1, bold=True, color=NAVY)
        r2 = p.add_run(text[len(bold_label):])
        set_run_font(r2)
    else:
        run = p.add_run(text)
        set_run_font(run)
    return p


def add_heading(doc, text, level=1):
    p = doc.add_paragraph(style=f"Heading {level}")
    p.paragraph_format.keep_with_next = True
    p.add_run(text)
    return p


def add_callout(doc, label, text):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.16)
    p.paragraph_format.right_indent = Inches(0.12)
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after = Pt(10)
    p.paragraph_format.line_spacing = 1.10
    shade_paragraph(p)
    set_paragraph_border(p, side="left", color=BLUE, size="18", space="8")
    r1 = p.add_run(f"{label}  ")
    set_run_font(r1, bold=True, color=NAVY)
    r2 = p.add_run(text)
    set_run_font(r2, color=INK)
    return p


def add_labeled_line(doc, label, value):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(4)
    p.paragraph_format.line_spacing = 1.10
    r1 = p.add_run(f"{label}：")
    set_run_font(r1, bold=True, color=NAVY)
    r2 = p.add_run(value)
    set_run_font(r2)
    return p


def write_cell(cell, text, *, bold=False, color=INK, size=9.5, align=WD_ALIGN_PARAGRAPH.LEFT):
    p = cell.paragraphs[0]
    p.alignment = align
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(0)
    p.paragraph_format.line_spacing = 1.10
    run = p.add_run(str(text))
    set_run_font(run, size=size, bold=bold, color=color)


def add_table(doc, headers, rows, widths, *, font_size=9.25):
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    for idx, header in enumerate(headers):
        write_cell(table.rows[0].cells[idx], header, bold=True, color=NAVY, size=font_size, align=WD_ALIGN_PARAGRAPH.CENTER)
        shade_cell(table.rows[0].cells[idx], LIGHT_GRAY)
    set_repeat_header(table.rows[0])
    for row_values in rows:
        row = table.add_row()
        for idx, value in enumerate(row_values):
            align = WD_ALIGN_PARAGRAPH.CENTER if idx == 0 or (isinstance(value, str) and value in {"完成", "通过", "已发布"}) else WD_ALIGN_PARAGRAPH.LEFT
            write_cell(row.cells[idx], value, size=font_size, align=align)
    set_table_geometry(table, widths)
    after = doc.add_paragraph()
    after.paragraph_format.space_after = Pt(4)
    return table


def add_task(doc, number, title, objective, completed, deliverables, verification, bullet_id):
    add_heading(doc, f"任务 {number}｜{title}", 1)
    add_labeled_line(doc, "任务目标", objective)
    add_heading(doc, "完成内容", 2)
    for item in completed:
        add_bullet(doc, item, bullet_id)
    add_heading(doc, "主要交付", 2)
    for item in deliverables:
        add_bullet(doc, item, bullet_id)
    add_callout(doc, "完成与验证", verification)


def setup_styles(doc):
    styles = doc.styles
    normal = styles["Normal"]
    normal.font.name = "Calibri"
    normal._element.rPr.rFonts.set(qn("w:ascii"), "Calibri")
    normal._element.rPr.rFonts.set(qn("w:hAnsi"), "Calibri")
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    normal.font.size = Pt(11)
    normal.font.color.rgb = RGBColor.from_string(INK)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.10

    heading_tokens = {
        1: (16, BLUE, 16, 8),
        2: (13, BLUE, 12, 6),
        3: (12, DARK_BLUE, 8, 4),
    }
    for level, (size, color, before, after) in heading_tokens.items():
        style = styles[f"Heading {level}"]
        style.font.name = "Calibri"
        style._element.rPr.rFonts.set(qn("w:ascii"), "Calibri")
        style._element.rPr.rFonts.set(qn("w:hAnsi"), "Calibri")
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = RGBColor.from_string(color)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.keep_with_next = True
        style.paragraph_format.widow_control = True


def setup_page(doc):
    section = doc.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(1)
    section.right_margin = Inches(1)
    section.bottom_margin = Inches(1)
    section.left_margin = Inches(1)
    section.header_distance = Inches(0.492)
    section.footer_distance = Inches(0.492)

    header_p = section.header.paragraphs[0]
    header_p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    header_p.paragraph_format.space_after = Pt(0)
    r = header_p.add_run("个人信审本体建模项目｜八项任务交付总结")
    set_run_font(r, size=8.5, color=GRAY)

    footer_p = section.footer.paragraphs[0]
    footer_p.paragraph_format.tab_stops.add_tab_stop(Inches(6.5), 2)
    footer_p.paragraph_format.space_before = Pt(0)
    footer_p.paragraph_format.space_after = Pt(0)
    left = footer_p.add_run("内部项目文档")
    set_run_font(left, size=8.5, color=GRAY)
    mid = footer_p.add_run("\t第 ")
    set_run_font(mid, size=8.5, color=GRAY)
    page_run = footer_p.add_run()
    set_run_font(page_run, size=8.5, color=GRAY)
    add_page_field(page_run)
    tail = footer_p.add_run(" 页")
    set_run_font(tail, size=8.5, color=GRAY)


def build_document():
    doc = Document()
    setup_styles(doc)
    setup_page(doc)
    bullet_id = add_bullet_numbering(doc)

    doc.core_properties.title = "个人信审本体八项任务总结"
    doc.core_properties.subject = "个人汽车信审静态本体 1.0.0 项目交付总结"
    doc.core_properties.author = "个人信审本体项目组"
    doc.core_properties.keywords = "个人信审, 本体, PostgreSQL, NebulaGraph, TBox, ABox"

    # First-page memo masthead.
    spacer = doc.add_paragraph()
    spacer.paragraph_format.space_after = Pt(22)
    kicker = doc.add_paragraph()
    kicker.paragraph_format.space_after = Pt(4)
    kr = kicker.add_run("PROJECT DELIVERY SUMMARY")
    set_run_font(kr, size=10, bold=True, color=BLUE)

    title = doc.add_paragraph()
    title.paragraph_format.space_before = Pt(0)
    title.paragraph_format.space_after = Pt(6)
    tr = title.add_run("个人信审本体建模项目")
    set_run_font(tr, size=26, bold=True, color="000000")

    subtitle = doc.add_paragraph()
    subtitle.paragraph_format.space_after = Pt(18)
    sr = subtitle.add_run("八项任务整理与交付总结")
    set_run_font(sr, size=15, color=DARK_BLUE)

    metadata = [
        ("项目范围", "个人汽车信贷贷前申请与信审评估"),
        ("正式版本", "1.0.0"),
        ("技术架构", "PostgreSQL + FastAPI + NebulaGraph + Web 可视化"),
        ("完成状态", "静态本体已发布"),
        ("整理日期", METRICS["as_of"]),
    ]
    for label, value in metadata:
        add_labeled_line(doc, label, value)
    rule = doc.add_paragraph()
    rule.paragraph_format.space_before = Pt(10)
    rule.paragraph_format.space_after = Pt(14)
    set_paragraph_border(rule, side="bottom", color=BLUE, size="14", space="4")

    add_callout(
        doc,
        "核心结论",
        "八项任务已形成从业务范围、字段与关系目录、约束公理、动力层声明，到确定性基线、深度审核和正式发布的完整闭环。当前交付物是个人汽车信审静态本体 1.0.0。",
    )

    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    add_heading(doc, "一、项目最终成果", 1)
    add_body(
        doc,
        "本项目将原有 MVP 的数据源统一为 PostgreSQL，将知识图谱存储统一为 NebulaGraph，并在此基础上完成个人汽车信审本体的业务建模、前端审核、发布和图谱探索能力。",
    )
    metrics = [
        ("ObjectType", str(MODEL_STATS["object_types"])),
        ("Property", str(MODEL_STATS["properties"])),
        ("LinkType", str(MODEL_STATS["link_types"])),
        ("业务公理", str(MODEL_STATS["axioms"])),
        ("TBox 节点", str(GRAPH_STATS["tbox_vertices"])),
        ("TBox 关系", str(GRAPH_STATS["tbox_edges"])),
        ("发布前深度审核", f"{AUDIT_STATS['checks_passed']}/{AUDIT_STATS['checks_total']} 通过"),
        ("正式发布版本", METRICS["release_version"]),
    ]
    add_table(doc, ["关键指标", "最终结果"], metrics, [5600, 3760], font_size=9.5)

    add_heading(doc, "二、八项任务一览", 1)
    overview_rows = [
        ("1", "锁定建模范围与对象体系", f"个人汽车信审范围、{MODEL_STATS['object_types']} 类业务对象及角色建模", "完成"),
        ("2", "建立属性与字段目录", f"{MODEL_STATS['properties']} 个属性映射 PostgreSQL {DATABASE_STATS['fields_total']} 个字段", "完成"),
        ("3", "建立关系目录", f"{MODEL_STATS['link_types']} 条关系及方向、基数、来源和约束", "完成"),
        ("4", "建立 Interface 与公理", f"{MODEL_STATS['interfaces']} 个 Interface、{MODEL_STATS['axioms']} 条业务公理", "完成"),
        ("5", "定义动力层与安全边界", f"{MODEL_STATS['functions']} Function、{MODEL_STATS['action_types']} ActionType、{MODEL_STATS['dynamic_security']} DynamicSecurity", "完成"),
        ("6", "生成确定性基线与可视化审核", "基线生成、AI 候选隔离、可视化编辑与预览", "完成"),
        ("7", "实施发布前深度审核", f"{AUDIT_STATS['checks_total']} 项审核、摘要绑定、前后端发布拦截", "完成"),
        ("8", "定稿并发布 1.0.0", "替换旧 0.1 TBox，写入可追溯发布元数据", "已发布"),
    ]
    add_table(doc, ["序号", "任务", "核心产出", "状态"], overview_rows, [720, 2700, 4500, 1440], font_size=9)

    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
    add_task(
        doc, 1, "锁定个人信审范围与对象体系",
        "将建模目标收敛为个人汽车信贷贷前申请与信审评估，避免将后续运行能力或企业借款业务提前混入 1.0.0。",
        [
            f"确定 {MODEL_STATS['object_types']} 类业务对象：自然人、经销商、车辆、贷款申请单、征信报告、外部存量负债、风险评估报告、反欺诈报告和电核记录。",
            "将借款人、担保人、共借人建模为 NaturalPerson 在具体 LoanApplication 中的关系角色，不建立重复对象类型。",
            "明确排除企业借款、贷后管理、CDC、实时推理、自动审批、MCP、正式 FIBO URI 对齐及运行态 ABox-TBox 自动映射。",
        ],
        [
            "PERSONAL_CREDIT_ONTOLOGY_SCOPE.md",
            "backend/app/ontology/scope.py",
        ],
        f"范围基线形成 {MODEL_STATS['object_types']} 个唯一 ObjectType，业务对象与角色边界清晰，成为后续属性、关系和公理目录的唯一范围依据。",
        bullet_id,
    )

    add_task(
        doc, 2, "建立属性目录与 PostgreSQL 字段映射",
        "为每个 ObjectType 定义可追溯到 PostgreSQL 的属性、主键、必填条件、枚举、单位和数据来源。",
        [
            f"建立 {MODEL_STATS['properties']} 个 Property，并精确映射到 {DATABASE_STATS['tables']} 张 PostgreSQL 业务表。",
            "将数据库必填与业务必填分开，增加 required、required_when、business_key、enum_values、unit、source_table、source_column 和 data_source 等元数据。",
            f"确认 {DATABASE_STATS['fields_total']} 个数据库字段中有 {DATABASE_STATS['fields_modeled']} 个进入本体；credit_report.subject_type 因当前范围固定为个人而有意排除。",
            "统一 NULL 语义：NULL 表示尚未采集或阶段未完成，不能与已确认不存在、空字符串或零值混用。",
        ],
        [
            "PERSONAL_CREDIT_ATTRIBUTE_CATALOG.md",
            "backend/app/ontology/attribute_catalog.py",
            "backend/app/ontology/models.py（Property 扩展）",
        ],
        f"字段覆盖结果为 {DATABASE_STATS['fields_modeled']} 个建模字段 + {DATABASE_STATS['fields_intentionally_excluded']} 个有意排除字段 = PostgreSQL 全部 {DATABASE_STATS['fields_total']} 个字段，无未决字段。",
        bullet_id,
    )

    add_task(
        doc, 3, "建立业务关系目录",
        "将人员角色、车辆、经销商、报告和关联风险组织为可审核的 LinkType 网络。",
        [
            f"定义 SUBMITS、GUARANTEES、CO_BORROWS、TARGETS、SOLD_BY、HOLDS、OWES、GENERATES、HAS_FRAUD_REPORT、HAS_PHONE_VERIFICATION、SHARES_CONTACT 共 {MODEL_STATS['link_types']} 条关系。",
            "为每条关系定义中文名称、源对象、目标对象、方向、源/目标基数、关系属性、来源字段和约束。",
            "将 SHARES_CONTACT 定义为由手机号或地址匹配产生的无向关联线索，明确其不等于同一人或欺诈结论。",
        ],
        [
            "PERSONAL_CREDIT_RELATION_CATALOG.md",
            "backend/app/ontology/relation_catalog.py",
            "backend/app/ontology/models.py（LinkType 扩展）",
        ],
        f"{MODEL_STATS['link_types']} 条关系名称唯一，源/目标对象均存在，{DATABASE_STATS['relation_source_fields']} 个去重后的关系来源字段均能在 PostgreSQL 中找到。",
        bullet_id,
    )

    add_task(
        doc, 4, "建立 Interface 与业务公理",
        "把身份、收入、车辆、征信、担保和申请完整性要求转化为结构化约束声明。",
        [
            f"定义 PersonalIdentity、IncomeSource、AssessableVehicle、CreditRecordHolder、GuaranteeCapablePerson、ApplicationIntegrity 共 {MODEL_STATS['interfaces']} 个 Interface。",
            f"形成 {MODEL_STATS['axioms']} 条公理，覆盖身份证/手机号格式、收入、VIN、价格、征信、角色互斥、申请频次、报告基数、风险阈值、欺诈评分和电核转人工等规则。",
            "公理具有唯一 ID、严重程度、适用对象和校验阶段，并已接入同步前批量数据校验；不虚构持续推理能力。",
        ],
        [
            "PERSONAL_CREDIT_INTERFACE_AXIOMS.md",
            "backend/app/ontology/interface_catalog.py",
            "backend/app/ontology/models.py（Interface / Axiom 扩展）",
        ],
        f"{MODEL_STATS['interfaces']} 个 Interface 引用的属性和关系全部存在，{MODEL_STATS['axioms']} 条公理 ID 唯一，接口实现对象与要求完整。",
        bullet_id,
    )

    add_task(
        doc, 5, "定义 Function、ActionType 与 DynamicSecurity",
        "建立动力层契约，同时明确当前阶段只声明能力、不绑定真实系统、不自动执行审批。",
        [
            f"定义 {MODEL_STATS['functions']} 个无副作用 Function：DTI、LTV、征信风险、欺诈风险、综合风险、电核问题、关联风险、决策支持和证据说明。",
            f"定义 {MODEL_STATS['action_types']} 个 ActionType：创建申请、征信查询、反欺诈查询、风险评估、电核任务、记录电核、人工复核和记录决策建议。",
            f"为 {MODEL_STATS['dynamic_security']} 个 ActionType 配置一一对应的 DynamicSecurity，规定允许角色、禁止角色、审批要求、敏感字段和审计边界。",
            "明确不存在 execute_approval 自动审批动作；所有 ActionType 均为 declaration_only，所有 Function 均为 not_bound。",
        ],
        [
            "PERSONAL_CREDIT_CAPABILITIES_SECURITY.md",
            "backend/app/ontology/capability_catalog.py",
            "backend/app/ontology/models.py（Function / ActionType / DynamicSecurity 扩展）",
        ],
        "校验确认函数无副作用、动作均要求审计、每个动作有且仅有一条安全策略，运行态能力开关保持关闭。",
        bullet_id,
    )

    add_task(
        doc, 6, "生成确定性基线并整合可视化审核",
        "将六份已审核目录组合为可重复生成的 OntologyProposal，并把 AI 调整为辅助候选入口。",
        [
            "实现 build_personal_credit_baseline()，确定性生成完整个人信审基线，避免依赖 LLM 才能得到正式结构。",
            "新增“生成个人信审基线”和“AI 辅助建议”：AI 结果进入独立候选区，不自动覆盖当前 JSON，必须人工采用。",
            f"可视化预览显示原名称、中文名称、数据示例、必填标识、{MODEL_STATS['link_types']} 条关系及基数；点击本体以表格查看全部属性。",
            "支持本体和关系的新建、编辑、删除及属性归属修改；任何修改都会要求重新校验。",
            "未使用字段区域区分真正遗漏字段与按范围有意排除字段。",
        ],
        [
            "backend/app/ontology/baseline.py",
            "backend/app/api/routes.py（/api/ontology/baseline）",
            "frontend/index.html",
        ],
        f"基线可稳定生成 {MODEL_STATS['object_types']} 个本体、{MODEL_STATS['properties']} 个属性、{MODEL_STATS['link_types']} 条关系、{MODEL_STATS['interfaces']} 个 Interface、{MODEL_STATS['axioms']} 条公理、{MODEL_STATS['functions']}/{MODEL_STATS['action_types']}/{MODEL_STATS['dynamic_security']} 个动力层与安全声明；页面无控制台错误。",
        bullet_id,
    )

    add_task(
        doc, 7, "实施发布前深度审核",
        "建立目录级、数据库级和安全边界级审核，确保只有当前完整 JSON 能获得发布资格。",
        [
            "新增独立深度审核器，对对象、属性、关系、Interface、公理、Function、ActionType、DynamicSecurity、元数据和 PostgreSQL 字段进行一致性检查。",
            "审核结果通过 SHA-256 摘要绑定当前 proposal；JSON 一旦编辑，前端审核凭证立即失效。",
            "版本审核和发布接口会在服务端重新执行门禁；旧的 /api/ontology/approve 直接发布入口返回 HTTP 409。",
            "加入发布版本标识检查；缺少属性或将版本改回草稿时审核失败，不能进入批准和发布状态。",
        ],
        [
            "backend/app/ontology/audit.py",
            "backend/app/api/routes.py（/api/ontology/audit 与发布硬校验）",
            "PERSONAL_CREDIT_PREPUBLICATION_AUDIT.md",
        ],
        f"最终 {METRICS['release_version']} 深度审核 {AUDIT_STATS['checks_passed']}/{AUDIT_STATS['checks_total']} 项通过，审核摘要为 {AUDIT_STATS['digest']}。",
        bullet_id,
    )

    add_task(
        doc, 8, "定稿并发布个人信审本体 1.0.0",
        "把候选基线转为正式版本，替换 NebulaGraph 中旧 TBox，并保留现有 ABox 业务实例。",
        [
            "将版本从 1.0.0-draft 定稿为 1.0.0，元数据状态调整为 approved。",
            "发布时删除旧 0.1 的 89 个 ontology 节点及其结构边，随后写入完整 1.0.0 TBox。",
            "在根节点记录 version、published 状态、审核摘要、目录版本和统计元数据。",
            f"当前 ABox 保留 {GRAPH_STATS['abox_entities']} 个业务实体，包含 {GRAPH_STATS['business_relation_edges']} 条业务关系和 {GRAPH_STATS['instance_of_edges']} 条 INSTANCE_OF 映射边，未修改 PostgreSQL 数据。",
        ],
        [
            "PERSONAL_CREDIT_ONTOLOGY_RELEASE_1.0.0.md",
            "backend/app/kg/nebula.py",
            "NebulaGraph 根节点 tbox:Ontology:credit_ontology",
        ],
        f"当前图谱包含 {GRAPH_STATS['tbox_vertices']} 个 TBox 节点、{GRAPH_STATS['tbox_edges']} 条 TBox 关系、{GRAPH_STATS['abox_entities']} 个 ABox 实体、{GRAPH_STATS['abox_edges_total']} 条 ABox 边；全量为 {GRAPH_STATS['total_vertices']} 个节点和 {GRAPH_STATS['total_edges']} 条关系。",
        bullet_id,
    )

    add_heading(doc, "附录 A｜本体核心清单", 1)
    add_heading(doc, "A.1 ObjectType 与属性数量", 2)
    object_rows = [
        ("NaturalPerson", "自然人", "11", "借款人、担保人、共借人的统一主体"),
        ("Dealer", "经销商", "3", "车辆销售与进件渠道参与方"),
        ("Vehicle", "车辆", "6", "贷款对应的车辆金融标的"),
        ("LoanApplication", "贷款申请单", "13", "连接人员、车辆、经销商和报告的核心枢纽"),
        ("CreditReport", "征信报告", "9", "个人征信查询与信用表现"),
        ("ExternalDebt", "外部存量负债", "6", "外部机构负债与逾期状态"),
        ("RiskAssessmentReport", "风险评估报告", "11", "评分、风险等级与风险因子"),
        ("AntiFraudReport", "反欺诈报告", "5", "欺诈评分、结果和命中明细"),
        ("PhoneVerificationRecord", "电核记录", "9", "电核过程、结论与存疑点"),
    ]
    add_table(doc, ["ObjectType", "中文名称", "属性数", "核心语义"], object_rows, [2600, 1700, 980, 4080], font_size=8.8)

    add_heading(doc, "A.2 LinkType 清单", 2)
    relation_rows = [
        ("SUBMITS", "提交申请", "NaturalPerson", "LoanApplication", "0..N:1"),
        ("GUARANTEES", "提供担保", "NaturalPerson", "LoanApplication", "0..N:0..1"),
        ("CO_BORROWS", "共同借款", "NaturalPerson", "LoanApplication", "0..N:0..1"),
        ("TARGETS", "针对车辆", "LoanApplication", "Vehicle", "1:0..N"),
        ("SOLD_BY", "来源经销商", "LoanApplication", "Dealer", "0..1:0..N"),
        ("HOLDS", "持有征信报告", "NaturalPerson", "CreditReport", "0..N:1"),
        ("OWES", "负有外部负债", "NaturalPerson", "ExternalDebt", "0..N:1"),
        ("GENERATES", "生成风险评估", "LoanApplication", "RiskAssessmentReport", "0..1:1"),
        ("HAS_FRAUD_REPORT", "关联反欺诈报告", "LoanApplication", "AntiFraudReport", "0..1:1"),
        ("HAS_PHONE_VERIFICATION", "具有电核记录", "LoanApplication", "PhoneVerificationRecord", "0..N:1"),
        ("SHARES_CONTACT", "同联系方式", "NaturalPerson", "NaturalPerson", "0..N:0..N"),
    ]
    add_table(doc, ["LinkType", "中文名称", "源对象", "目标对象", "基数"], relation_rows, [2100, 1700, 1900, 2200, 1460], font_size=8.2)

    add_heading(doc, "附录 B｜主要交付文件", 1)
    deliverable_rows = [
        ("范围基线", "PERSONAL_CREDIT_ONTOLOGY_SCOPE.md", "业务范围与对象边界"),
        ("属性目录", "PERSONAL_CREDIT_ATTRIBUTE_CATALOG.md", f"{MODEL_STATS['properties']} 个属性与字段口径"),
        ("关系目录", "PERSONAL_CREDIT_RELATION_CATALOG.md", f"{MODEL_STATS['link_types']} 条关系、方向与基数"),
        ("公理目录", "PERSONAL_CREDIT_INTERFACE_AXIOMS.md", f"{MODEL_STATS['interfaces']} 个 Interface、{MODEL_STATS['axioms']} 条公理"),
        ("动力与安全", "PERSONAL_CREDIT_CAPABILITIES_SECURITY.md", "Function、ActionType、安全策略"),
        ("深度审核", "PERSONAL_CREDIT_PREPUBLICATION_AUDIT.md", f"{AUDIT_STATS['checks_total']} 项审核与反例验证"),
        ("发布说明", "PERSONAL_CREDIT_ONTOLOGY_RELEASE_1.0.0.md", "1.0.0 发布与图谱状态"),
        ("范围实现", "backend/app/ontology/scope.py", "对象与范围配置"),
        ("目录实现", "backend/app/ontology/*_catalog.py", "属性、关系、公理、能力目录"),
        ("基线与审核", "backend/app/ontology/baseline.py / audit.py", "确定性生成与深度审核"),
        ("图存储实现", "backend/app/kg/nebula.py", "TBox 发布与 ABox 同步"),
        ("前端实现", "frontend/index.html", "本体构建、预览与图谱探索"),
    ]
    add_table(doc, ["类别", "文件", "用途"], deliverable_rows, [1800, 4700, 2860], font_size=8.5)

    add_heading(doc, "附录 C｜当前版本能力边界", 1)
    add_body(
        doc,
        "个人信审本体 1.0.0 已完成静态建模和发布，但以下内容不属于八项任务的已实现能力，文档中均按真实状态保留边界。",
    )
    boundaries = [
        "Function 与 ActionType 仍为无副作用、未绑定和声明态，不执行自动审批或外部系统动作。",
        "CDC、事件驱动增量更新、双版本原子切换、不兼容迁移编排、真实业务数据验证和正式 FIBO 对齐尚未实施。",
        "真实个人信审数据接入前仍需完成字段复核、脱敏授权、数据血缘、时态信息和属性级隐私治理。",
    ]
    for item in boundaries:
        add_bullet(doc, item, bullet_id)
    add_callout(
        doc,
        "项目状态",
        "八项任务已经完成并形成正式交付。若后续进入真实业务数据阶段，应优先实施真实字段复核、增量更新、时态治理和数据隐私治理，而不是继续无边界新增本体概念。",
    )

    doc.save(OUTPUT)
    return OUTPUT


if __name__ == "__main__":
    print(build_document())
