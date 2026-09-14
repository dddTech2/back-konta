import re
from dian_automation import config

SAMPLE_TABLE_EXPORT_HTML = """
<table colspan="12" class="simple-table documents-table table table-striped table-hover align-middle margin-bottom-0" data-int="0" id="tableExport">
    <thead>
        <tr>
            <th>Fecha</th>
            <th>Usuario</th>
            <th>Rango</th>
            <th>Grupo</th>
            <th>Tipo</th>
            <th class="text-left">Total</th>
            <th class="text-left">Estado</th>
            <th class="text-left">Acciones</th>
        </tr>
    </thead>
    <tbody>
        <tr>
            <td>14-09-2026</td>
            <td>10000002@dian.co</td>
            <td>Desde 01-08-2026 Hasta 31-08-2026</td>
            <td>Enviados y Recibidos</td>
            <td><i class="fa fa-file-excel-o"></i> Excel</td>
            <td class="text-left">701</td>
            <td class="text-left">
                <i class="fa fa-check fa-lg text-gosocket add-tooltip" title="Listo" data-html="true" data-original-title="Listo"></i>
            </td>
            <td class="text-left">
                <a class="btn btn-xs btn-hover-gosocket add-tooltip" href="/Document/DownloadExportedZipFile?pk=901008579&amp;rk=00000000-0000-0000-0000-000000000000" title="Descargar" data-html="true" data-original-title="Descargar">
                    <i class="fa fa-download"></i>
                </a>
            </td>
        </tr>
    </tbody>
</table>
"""

def test_download_selectors_defined():
    assert config.selectors.TABLE_EXPORT == "#tableExport"
    assert "DownloadExportedZipFile" in config.selectors.DOWNLOAD_LINK
    assert "Listo" in config.selectors.STATUS_READY_ICON

def test_extract_download_link_from_html():
    from html.parser import HTMLParser
    
    # Regex test to verify download link parsing
    match = re.search(r'href="([^"]*DownloadExportedZipFile[^"]*)"', SAMPLE_TABLE_EXPORT_HTML)
    assert match is not None
    link = match.group(1)
    assert "DownloadExportedZipFile" in link
    assert "pk=901008579" in link
    assert "rk=00000000-0000-0000-0000-000000000000" in link

def test_match_date_range_in_table_row():
    start_fmt = "01-08-2026"
    end_fmt = "31-08-2026"
    expected_range = f"Desde {start_fmt} Hasta {end_fmt}"
    assert expected_range in SAMPLE_TABLE_EXPORT_HTML
