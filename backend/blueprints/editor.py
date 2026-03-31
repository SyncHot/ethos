"""
EthOS — Document Editor
Backend API for opening/saving DOCX and exporting PDF
"""

from flask import Blueprint, request, jsonify, send_file, g
import os
import io
import re
import subprocess
import tempfile
import mammoth
from docx import Document
from docx.shared import Pt, Inches, RGBColor, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from htmldocx import HtmlToDocx

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import NATIVE_MODE, ensure_dep, check_dep
from utils import safe_path as _safe_path_util, register_pkg_routes

editor_bp = Blueprint('editor', __name__)


def _safe_path(user_path):
    sudo = getattr(g, 'sudo_mode', False)
    return _safe_path_util(user_path, isolate_home=False, sudo_mode=sudo)


# ─── Open DOCX → HTML ───

@editor_bp.route('/api/editor/open', methods=['POST'])
def editor_open():
    """Open a .docx file and return its content as HTML."""
    data = request.json or {}
    path = data.get('path', '')
    real = _safe_path(path)
    if not real or not os.path.isfile(real):
        return jsonify({'error': 'File not found'}), 404

    ext = os.path.splitext(real)[1].lower()

    if ext == '.docx':
        try:
            with open(real, 'rb') as f:
                result = mammoth.convert_to_html(f, convert_image=mammoth.images.img_element(
                    _convert_image_inline
                ))
            html = result.value
            messages = [str(m) for m in result.messages]
            return jsonify({
                'ok': True,
                'html': html,
                'filename': os.path.basename(real),
                'path': path,
                'messages': messages
            })
        except Exception as e:
            return jsonify({'error': f'DOCX read error: {str(e)}'}), 500
    elif ext == '.txt':
        try:
            with open(real, 'r', encoding='utf-8', errors='replace') as f:
                text = f.read()
            html = f'<p>{_escape_html(text).replace(chr(10), "</p><p>")}</p>'
            return jsonify({
                'ok': True,
                'html': html,
                'filename': os.path.basename(real),
                'path': path,
                'messages': []
            })
        except Exception as e:
            return jsonify({'error': f'Read error: {str(e)}'}), 500
    elif ext == '.html' or ext == '.htm':
        try:
            with open(real, 'r', encoding='utf-8', errors='replace') as f:
                html = f.read()
            # Extract body content if full HTML
            body_match = re.search(r'<body[^>]*>(.*?)</body>', html, re.DOTALL | re.IGNORECASE)
            if body_match:
                html = body_match.group(1)
            return jsonify({
                'ok': True,
                'html': html,
                'filename': os.path.basename(real),
                'path': path,
                'messages': []
            })
        except Exception as e:
            return jsonify({'error': f'Read error: {str(e)}'}), 500
    else:
        return jsonify({'error': f'Unsupported format: {ext}'}), 400


def _convert_image_inline(image):
    """Convert embedded images to inline base64 data URIs."""
    with image.open() as img_data:
        data = img_data.read()
    import base64
    ct = image.content_type or 'image/png'
    b64 = base64.b64encode(data).decode('ascii')
    return {'src': f'data:{ct};base64,{b64}'}


def _escape_html(text):
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


# ─── Save as DOCX ───

@editor_bp.route('/api/editor/save-docx', methods=['POST'])
def editor_save_docx():
    """Save HTML content as a .docx file."""
    data = request.json or {}
    html = data.get('html', '')
    path = data.get('path', '')
    filename = data.get('filename', 'dokument.docx')

    if not html:
        return jsonify({'error': 'No content to save'}), 400

    # Ensure .docx extension
    if not filename.lower().endswith('.docx'):
        filename = os.path.splitext(filename)[0] + '.docx'

    # Wrap HTML for better conversion
    full_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
body {{ font-family: 'Calibri', 'Arial', sans-serif; font-size: 11pt; line-height: 1.5; }}
table {{ border-collapse: collapse; width: 100%; }}
td, th {{ border: 1px solid #ccc; padding: 6px 10px; }}
img {{ max-width: 100%; }}
</style></head><body>{html}</body></html>"""

    try:
        doc = Document()

        # Set default font
        style = doc.styles['Normal']
        font = style.font
        font.name = 'Calibri'
        font.size = Pt(11)

        # Set margins
        for section in doc.sections:
            section.top_margin = Cm(2.5)
            section.bottom_margin = Cm(2.5)
            section.left_margin = Cm(2.5)
            section.right_margin = Cm(2.5)

        # Convert HTML to DOCX
        parser = HtmlToDocx()
        parser.add_html_to_document(full_html, doc)

        # Save to path or send as download
        if path:
            real = _safe_path(path)
            if not real:
                return jsonify({'error': 'Invalid path'}), 400
            # Save in the same directory
            save_dir = os.path.dirname(real) if os.path.isfile(real) else real
            if not os.path.isdir(save_dir):
                return jsonify({'error': 'Target folder not found'}), 400
            save_path = os.path.join(save_dir, filename)
            doc.save(save_path)
            # Compute user-visible path
            user_save = save_path if save_path.startswith('/') else '/' + save_path
            return jsonify({'ok': True, 'saved_path': user_save, 'filename': filename})
        else:
            # Return as download
            buf = io.BytesIO()
            doc.save(buf)
            buf.seek(0)
            return send_file(buf, mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                             download_name=filename, as_attachment=True)
    except Exception as e:
        return jsonify({'error': f'DOCX write error: {str(e)}'}), 500


# ─── Save as PDF ───

@editor_bp.route('/api/editor/save-pdf', methods=['POST'])
def editor_save_pdf():
    """Export HTML content as PDF using WeasyPrint (produces text-extractable PDFs)."""
    data = request.json or {}
    html = data.get('html', '')
    path = data.get('path', '')
    filename = data.get('filename', 'dokument.pdf')

    if not html:
        return jsonify({'error': 'No content to export'}), 400

    if not filename.lower().endswith('.pdf'):
        filename = os.path.splitext(filename)[0] + '.pdf'

    full_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
@page {{ size: A4; margin: 2cm; }}
body {{ font-family: 'Liberation Sans', 'DejaVu Sans', 'Calibri', Arial, sans-serif; font-size: 11pt; line-height: 1.6; color: #1a1a1a; }}
h1 {{ font-size: 22pt; margin-top: 0; }}
h2 {{ font-size: 16pt; }}
h3 {{ font-size: 13pt; }}
table {{ border-collapse: collapse; width: 100%; }}
td, th {{ border: 1px solid #ccc; padding: 6px 10px; }}
img {{ max-width: 100%; }}
ul, ol {{ padding-left: 24pt; }}
blockquote {{ border-left: 3px solid #ccc; margin-left: 0; padding-left: 12pt; color: #555; }}
</style></head><body>{html}</body></html>"""

    try:
        from weasyprint import HTML as WeasyHTML
        pdf_bytes = WeasyHTML(string=full_html).write_pdf()

        if path:
            real = _safe_path(path)
            if not real:
                return jsonify({'error': 'Invalid path'}), 400
            save_dir = os.path.dirname(real) if os.path.isfile(real) else real
            if not os.path.isdir(save_dir):
                return jsonify({'error': 'Target folder not found'}), 400
            save_path = os.path.join(save_dir, filename)
            with open(save_path, 'wb') as f:
                f.write(pdf_bytes)
            user_save = save_path if save_path.startswith('/') else '/' + save_path
            return jsonify({'ok': True, 'saved_path': user_save, 'filename': filename})
        else:
            buf = io.BytesIO(pdf_bytes)
            buf.seek(0)
            return send_file(buf, mimetype='application/pdf', download_name=filename, as_attachment=True)
    except Exception as e:
        return jsonify({'error': f'PDF export error: {str(e)}'}), 500


# ─── Save overwrite (same file) ───

@editor_bp.route('/api/editor/save', methods=['POST'])
def editor_save():
    """Overwrite the original file with updated content."""
    data = request.json or {}
    html = data.get('html', '')
    path = data.get('path', '')

    if not html or not path:
        return jsonify({'error': 'Content or path required'}), 400

    real = _safe_path(path)
    if not real:
        return jsonify({'error': 'Invalid path'}), 400

    ext = os.path.splitext(real)[1].lower()

    try:
        if ext == '.docx':
            full_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
body {{ font-family: 'Calibri', 'Arial', sans-serif; font-size: 11pt; line-height: 1.5; }}
table {{ border-collapse: collapse; width: 100%; }}
td, th {{ border: 1px solid #ccc; padding: 6px 10px; }}
img {{ max-width: 100%; }}
</style></head><body>{html}</body></html>"""

            doc = Document()
            style = doc.styles['Normal']
            style.font.name = 'Calibri'
            style.font.size = Pt(11)
            for section in doc.sections:
                section.top_margin = Cm(2.5)
                section.bottom_margin = Cm(2.5)
                section.left_margin = Cm(2.5)
                section.right_margin = Cm(2.5)

            parser = HtmlToDocx()
            parser.add_html_to_document(full_html, doc)
            doc.save(real)

        elif ext in ('.html', '.htm'):
            with open(real, 'w', encoding='utf-8') as f:
                f.write(html)

        elif ext == '.txt':
            # Strip HTML tags for plain text
            import re as _re
            text = _re.sub(r'<br\s*/?>', '\n', html)
            text = _re.sub(r'</p>\s*<p[^>]*>', '\n\n', text)
            text = _re.sub(r'<[^>]+>', '', text)
            text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&nbsp;', ' ')
            with open(real, 'w', encoding='utf-8') as f:
                f.write(text.strip())

        else:
            return jsonify({'error': f'Unsupported format: {ext}'}), 400

        return jsonify({'ok': True, 'path': path})
    except Exception as e:
        return jsonify({'error': f'Write error: {str(e)}'}), 500


# ── Package: install / uninstall / status ──

register_pkg_routes(
    editor_bp,
    install_message='Document editor ready.',
    install_deps=['libreoffice'],
    url_prefix='/api/editor',
)
