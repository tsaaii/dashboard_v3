"""
Weighbridge ticket PDF — enhanced_pdf_creator_jmd.create_pdf_report, lifted as
is. Styles, column widths, row heights, spacers, paddings and the placeholder
strings are the original's. Only three things differ, all agreed:

  * address   -> "{site_name} Dump yard, {site_name}, Andhra Pradesh"
                 (site_name from the API record; the original hard-coded JMD)
  * images    -> raw JPEG bytes from the records API, placed as-is. No
                 watermark, no resize, no timestamp offset: the captures
                 already carry the camera watermark.
  * output    -> bytes in memory, not a file on disk; no file-mtime rewrite.

The unrendered signature table at the end is kept commented out, as in the
original.
"""
from __future__ import annotations

import io
import logging

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Image as RLImage, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

logger = logging.getLogger(__name__)


def get_dynamic_address_info(agency_name, site_name):
    """Address per site, from the API's site_name."""
    return {
        "name": agency_name,
        "address": f"{site_name} Dump yard, {site_name}, Andhra Pradesh",
        "contact": "",
        "email": "",
    }


def _rl_image(blob: bytes | None):
    """RLImage at the original's fixed 3.5 x 2.0 inch, or None."""
    if not blob:
        return None
    try:
        return RLImage(io.BytesIO(blob), width=3.5 * inch, height=2.0 * inch)
    except Exception as e:                                       # noqa: BLE001
        logger.error(f"Error processing image: {e}")
        return None


def build(record_data: dict, images: dict) -> bytes:
    """
    Create PDF report with 4-image grid for a single record
    Uses improved tabular formatting for weighment details

    Args:
        record_data: Weighbridge record data from the API
        images: {slot: jpeg bytes or None} for first_front, first_back,
                second_front, second_back
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)

    styles = getSampleStyleSheet()  # noqa: F841  (kept as in the original)
    elements = []

    # Ink-friendly styles with increased font sizes (improved styling)
    header_style = ParagraphStyle(
        name='HeaderStyle',
        fontSize=18,
        alignment=TA_CENTER,
        fontName='Helvetica-Bold',
        textColor=colors.black,
        spaceAfter=6,
        spaceBefore=6
    )

    subheader_style = ParagraphStyle(
        name='SubHeaderStyle',
        fontSize=12,
        alignment=TA_CENTER,
        fontName='Helvetica',
        textColor=colors.black,
        spaceAfter=12
    )

    section_header_style = ParagraphStyle(
        name='SectionHeader',
        fontSize=13,
        alignment=TA_CENTER,
        fontName='Helvetica-Bold',
        textColor=colors.black,
        spaceAfter=6,
        spaceBefore=6
    )

    label_style = ParagraphStyle(
        name='LabelStyle',
        fontSize=11,
        fontName='Helvetica-Bold',
        textColor=colors.black
    )

    value_style = ParagraphStyle(
        name='ValueStyle',
        fontSize=11,
        fontName='Helvetica',
        textColor=colors.black
    )

    # Get agency information dynamically
    agency_name = record_data.get('agency_name', 'Unknown Agency')
    site_name = record_data.get('site_name', 'Unknown Site')
    agency_info = get_dynamic_address_info(agency_name, site_name)

    # Header Section with Agency Info
    elements.append(Paragraph(agency_info.get('name', agency_name), header_style))

    if agency_info.get('address'):
        address_text = agency_info.get('address', '').replace('/n', '<br/>')
        elements.append(Paragraph(address_text, subheader_style))

    # Contact information
    contact_info = []
    if agency_info.get('contact'):
        contact_info.append(f"Phone: {agency_info.get('contact')}")
    if agency_info.get('email'):
        contact_info.append(f"Email: {agency_info.get('email')}")

    if contact_info:
        elements.append(Paragraph(" | ".join(contact_info), subheader_style))

    elements.append(Spacer(1, 0.2*inch))

    # Print date from second_timestamp and ticket information
    print_date = (record_data.get('date', '') or '') + ' ' + (record_data.get('time', '') or '')
    ticket_no = record_data.get('ticket_no', '000')

    elements.append(Paragraph(f"Print Date: {print_date}", value_style))
    elements.append(Paragraph(f"Ticket No: {ticket_no}", header_style))
    elements.append(Spacer(1, 0.15*inch))

    # Vehicle Information
    elements.append(Paragraph("VEHICLE INFORMATION", section_header_style))

    # Get material from material_type field if material is empty
    material_value = record_data.get('material', '') or record_data.get('material_type', '') or ''
    user_name_value = record_data.get('user_name', '') or "Not specified"
    site_incharge_value = record_data.get('site_incharge', '') or "Not specified"

    def _s(key):
        return str(record_data.get(key, '') or '')

    vehicle_data = [
        [Paragraph("<b>Vehicle No:</b>", label_style), Paragraph(_s('vehicle_no'), value_style),
        Paragraph("<b>Date:</b>", label_style), Paragraph(_s('date'), value_style),
        Paragraph("<b>Time:</b>", label_style), Paragraph(_s('time'), value_style)],
        [Paragraph("<b>Material:</b>", label_style), Paragraph(material_value, value_style),
        Paragraph("<b>Site Name:</b>", label_style), Paragraph(_s('site_name'), value_style),
        Paragraph("<b>Transfer Party:</b>", label_style), Paragraph(_s('transfer_party_name'), value_style)],
        [Paragraph("<b>Agency Name:</b>", label_style), Paragraph(_s('agency_name'), value_style),
        Paragraph("<b>User Name:</b>", label_style), Paragraph(user_name_value, value_style),
        Paragraph("<b>Site Incharge:</b>", label_style), Paragraph(site_incharge_value, value_style)]
    ]

    vehicle_inner_table = Table(vehicle_data, colWidths=[1.2*inch, 1.3*inch, 1.0*inch, 1.3*inch, 1.2*inch, 1.5*inch])
    vehicle_inner_table.setStyle(TableStyle([
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica'),
        ('FONTSIZE', (0,0), (-1,-1), 13),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('LEFTPADDING', (0,0), (-1,-1), 2),
        ('RIGHTPADDING', (0,0), (-1,-1), 2),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
    ]))

    vehicle_table = Table([[vehicle_inner_table]], colWidths=[7.5*inch])
    vehicle_table.setStyle(TableStyle([
        ('GRID', (0,0), (-1,-1), 1, colors.black),
        ('LEFTPADDING', (0,0), (-1,-1), 12),
        ('RIGHTPADDING', (0,0), (-1,-1), 12),
        ('TOPPADDING', (0,0), (-1,-1), 8),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
    ]))
    elements.append(vehicle_table)
    elements.append(Spacer(1, 0.15*inch))

    # Weighment Information - IMPROVED TABULAR FORMAT
    elements.append(Paragraph("WEIGHMENT DETAILS", section_header_style))

    # Handle weights - convert to string if numeric
    first_weight_str = str(record_data.get('first_weight', '') if record_data.get('first_weight') is not None else '').strip()
    second_weight_str = str(record_data.get('second_weight', '') if record_data.get('second_weight') is not None else '').strip()
    net_weight_str = str(record_data.get('net_weight', '') if record_data.get('net_weight') is not None else '').strip()

    # Calculate net weight if not available
    if not net_weight_str or net_weight_str in ['None', 'null', ''] and first_weight_str and second_weight_str:
        try:
            first_weight = float(first_weight_str)
            second_weight = float(second_weight_str)
            calculated_net = abs(first_weight - second_weight)
            net_weight_str = f"{calculated_net:.2f}"
        except (ValueError, TypeError):
            net_weight_str = "Calculation Error"

    # If we still don't have net weight, try to calculate from available data
    if not net_weight_str or net_weight_str == "Calculation Error":
        if first_weight_str and second_weight_str:
            try:
                first_weight = float(first_weight_str)
                second_weight = float(second_weight_str)
                calculated_net = abs(first_weight - second_weight)
                net_weight_str = f"{calculated_net:.2f}"
            except (ValueError, TypeError):
                net_weight_str = "Unable to calculate"
        else:
            net_weight_str = "Not Available"

    # Weights always with two decimals (10300 -> 10300.00)
    def _two(v: str) -> str:
        try:
            return f"{float(v):.2f}"
        except (TypeError, ValueError):
            return v
    first_weight_str, second_weight_str = _two(first_weight_str), _two(second_weight_str)
    if net_weight_str not in ["Not Available", "Unable to calculate", "Calculation Error"]:
        net_weight_str = _two(net_weight_str)

    # Format display weights
    first_weight_display = f"{first_weight_str} kg" if first_weight_str else "Not captured"
    second_weight_display = f"{second_weight_str} kg" if second_weight_str else "Not captured"
    net_weight_display = f"{net_weight_str} kg" if net_weight_str and net_weight_str not in ["Not Available", "Unable to calculate", "Calculation Error"] else net_weight_str

    weighment_data = [
        [Paragraph("First Weight:", value_style), Paragraph(first_weight_display, value_style),
        Paragraph("First Time:", value_style), Paragraph(_s('first_timestamp') or "Not captured", value_style)],
        [Paragraph("Second Weight:", value_style), Paragraph(second_weight_display, value_style),
        Paragraph("Second Time:", value_style), Paragraph(_s('second_timestamp') or "Not captured", value_style)],
        ["", "", Paragraph("<b>Net Weight:</b>", label_style), Paragraph(f"<b>{net_weight_display}</b>", value_style)]
    ]

    weighment_inner_table = Table(weighment_data, colWidths=[1.5*inch, 1.5*inch, 1.2*inch, 2.8*inch])
    weighment_inner_table.setStyle(TableStyle([
        # Darker grid lines between all cells
        ('GRID', (0,0), (-1,-1), 1, colors.black),
        # Font settings
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica'),
        ('FONTSIZE', (0,0), (-1,-1), 12),
        # Net Weight label only - bold and bigger font
        ('FONTNAME', (2,2), (2,2), 'Helvetica-Bold'),
        ('FONTSIZE', (2,2), (2,2), 14),
        # Alignment
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        # Padding
        ('LEFTPADDING', (0,0), (-1,-1), 2),
        ('RIGHTPADDING', (0,0), (-1,-1), 2),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
    ]))

    weighment_table = Table([[weighment_inner_table]], colWidths=[7.5*inch])
    weighment_table.setStyle(TableStyle([
        # Match vehicle table padding - but no outer grid
        ('LEFTPADDING', (0,0), (-1,-1), 12),
        ('RIGHTPADDING', (0,0), (-1,-1), 12),
        ('TOPPADDING', (0,0), (-1,-1), 8),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
    ]))
    elements.append(weighment_table)
    elements.append(Spacer(1, 0.15*inch))

    # 4-Image Grid Section
    elements.append(Paragraph("VEHICLE IMAGES (4-Image System)", section_header_style))

    # Create 2x2 image grid with headers - IMPROVED DIMENSIONS
    img_data = [
        ["1ST WEIGHMENT - FRONT", "1ST WEIGHMENT - BACK"],
        [None, None],  # Will be filled with first weighment images
        ["2ND WEIGHMENT - FRONT", "2ND WEIGHMENT - BACK"],
        [None, None]   # Will be filled with second weighment images
    ]

    first_front_img = _rl_image(images.get('first_front'))
    if first_front_img is None:
        first_front_img = "1st Front/nImage not available"

    first_back_img = _rl_image(images.get('first_back'))
    if first_back_img is None:
        first_back_img = "1st Back/nImage not available"

    second_front_img = _rl_image(images.get('second_front'))
    if second_front_img is None:
        second_front_img = "2nd Front/nImage not available"

    second_back_img = _rl_image(images.get('second_back'))
    if second_back_img is None:
        second_back_img = "2nd Back/nImage not available"

    # Fill the image grid
    img_data[1] = [first_front_img, first_back_img]
    img_data[3] = [second_front_img, second_back_img]

    # Create images table with 2x2 grid
    img_table = Table(img_data, colWidths=[3.5*inch, 3.5*inch],
                     rowHeights=[0.3*inch, 2*inch, 0.3*inch, 2*inch])
    img_table.setStyle(TableStyle([
        ('GRID', (0,0), (-1,-1), 0.5, colors.black),
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (1,0), 10),  # Header row 1
        ('FONTSIZE', (0,2), (1,2), 10),  # Header row 2
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('RIGHTPADDING', (0,0), (-1,-1), 6),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        # Header background
        ('BACKGROUND', (0,0), (1,0), colors.lightgrey),
        ('BACKGROUND', (0,2), (1,2), colors.lightgrey),
    ]))
    elements.append(img_table)

    # Add operator signature line at bottom right
    elements.append(Spacer(1, 0.3*inch))

    signature_table = Table([["", ""]], colWidths=[5*inch, 2.5*inch])
    signature_table.setStyle(TableStyle([
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica'),
        ('FONTSIZE', (0,0), (-1,-1), 11),
        ('ALIGN', (1,0), (1,0), 'RIGHT'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('LEFTPADDING', (0,0), (-1,-1), 0),
        ('RIGHTPADDING', (0,0), (-1,-1), 0),
        ('TOPPADDING', (0,0), (-1,-1), 0),
        ('BOTTOMPADDING', (0,0), (-1,-1), 0),
    ]))
    #elements.append(signature_table)

    # Build the PDF
    doc.build(elements)
    return buf.getvalue()
