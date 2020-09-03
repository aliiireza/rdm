import os
import subprocess

import yaml

from rdm.image_extractor import image_is_svg, create_download_filters, create_relative_path_filter, \
    extract_image_url_sequence_from_tex
from rdm.util import determine_locations, filter_list_filter, create_filter_applicator, determine_relative_path


def yaml_gfm_to_tex(
    input_filename,
    context,
    output_file,
    download_to=None,
    output_base=None,
):
    '''
    This function uses Pandoc to convert our Github flavored markdown into
    latex.  We then alter this latex and insert a title, headers, etc. based on
    the yaml front matter.  A lot of the code in this module is fragile because
    it depends on the precise formatting of the Latex document generated by
    Pandoc, but it should work well enough for now.
    '''
    # From the command line arguments, decide where everything is located.
    input_folder, output_base, output_file = determine_locations(input_filename, output_file, output_base)

    # Grab the input
    with open(input_filename, 'r') as input_file:
        input_text = input_file.read()
    markdown, front_matter = _extract_yaml_front_matter(input_text)
    tex = _convert_with_pandoc(markdown)
    tex_lines = tex.split('\n')

    # Make necessary additions
    add_margins(tex_lines, front_matter, context)
    add_title_and_toc(tex_lines, front_matter, context)
    add_header_and_footer(tex_lines, front_matter, context)

    # Filter the result so far for graphics images.
    line_filter = create_image_handling_filter(input_folder, output_base, download_to)
    tex_lines = [line_filter(source_line) for source_line in tex_lines]

    # All done with creation. Write it to the destination.
    full_text = '\n'.join(tex_lines)
    if isinstance(output_file, str):
        with open(output_file, 'w') as output_stream:
            output_stream.write(full_text)
    else:
        output_file.write(full_text)


def _extract_yaml_front_matter(raw_string):
    parts = raw_string.split('---\n')
    if len(parts) < 3:
        raise ValueError('Invalid YAML front matter')
    front_matter_string = parts[1]
    template_string = '---\n'.join(parts[2:])
    try:
        front_matter = yaml.load(front_matter_string, Loader=yaml.SafeLoader)
    except yaml.YAMLError as e:
        raise ValueError('Invalid YAML front matter; improperly formatted YAML: {}'.format(e))
    return template_string, front_matter


def _convert_with_pandoc(markdown):
    p = subprocess.run(
        ['pandoc', '-f', 'gfm', '-t', 'latex', '--standalone',
         '-V', 'urlcolor=blue', '-V', 'linkcolor=black'],
        input=markdown,
        encoding='utf-8',
        stdout=subprocess.PIPE,
        universal_newlines=True
    )
    if p.returncode != 0:
        raise ValueError('Pandoc failed to convert markdown to latex')
    else:
        return p.stdout


def add_title_and_toc(tex_lines, front_matter, context):
    begin_document_index = tex_lines.index(r'\begin{document}')
    _insert_liness(tex_lines, begin_document_index + 1, [
        r'\maketitle',
        r'\thispagestyle{empty}',
        r'\tableofcontents',
        r'\pagebreak',
    ])
    # TODO: consider adding more useful error messages if keys are missing
    _insert_liness(tex_lines, begin_document_index, [
        r'\title{' + front_matter['title'] + r' \\ ',
        r'\large ' + front_matter['id'] + _revision_str(front_matter.get('revision')) + '}',
        r'\date{\today}',
        r'\author{' + context['system']['manufacturer_name'] + '}',
    ])


def add_header_and_footer(tex_lines, front_matter, context):
    begin_document_index = tex_lines.index(r'\begin{document}')
    _insert_liness(tex_lines, begin_document_index + 1, [
        r'\thispagestyle{empty}',
    ])
    _insert_liness(tex_lines, begin_document_index, [
        r'\usepackage{fancyhdr}',
        r'\usepackage{lastpage}',
        r'\pagestyle{fancy}',
        r'\lhead{' + front_matter['title'] + '}',
        r'\rhead{' + front_matter['id'] + _revision_str(front_matter.get('revision')) + '}',
        r'\cfoot{Page \thepage\ of \pageref{LastPage}}',
    ])


def _revision_str(revision_number):
    if revision_number is not None:
        return ', Rev. ' + str(revision_number)
    else:
        return ''


def add_margins(tex_lines, front_matter, context):
    try:
        document_class_index = tex_lines.index(r'\documentclass[]{article}')
    except ValueError:
        document_class_index = tex_lines.index(r'\documentclass[')
        if tex_lines[document_class_index + 1] == ']{article}':
            document_class_index += 1
        else:
            raise
    tex_lines.insert(document_class_index + 1, r'\usepackage[margin=1.25in]{geometry}')


def _insert_liness(existing, index, new_lines):
    for line in reversed(new_lines):
        existing.insert(index, line)


def create_image_handling_filter(input_folder, output_base, download_to):
    '''
    We want to support including images in two contexts:

    1. GitHub flavored markdown
    2. Inside PDF documents

    Each context has conflicting constraints. We translate between each approach
    as best we can here.

    The markdown allows URLs to images hosted elsewhere, while
    LaTeX does not.  We solve this by downloading images.

    The markdown supports SVGs, while LaTeX does not.  Thus, we convert SVGs
    into PDFs, and save them within `./tmp`.  Note that the SVG to PDF
    conversion is not perfect, and that there are some features of SVGs that are not supported, such as:

    - Masks
    - Style sheets
    - Color gradients
    - Embedded bitmaps
    '''

    # Where should converted svg files be placed?
    if download_to is None:
        svg_to_pdf_location = output_base
    else:
        svg_to_pdf_location = download_to

    # First translate relative paths
    relative_path_filter = create_relative_path_filter(input_folder, output_base)

    # Next do any downloads of remote urls (empty list if download_to == None)
    download_filters = create_download_filters(download_to, output_base)

    # Last convert any svg files to pdf files
    svg_to_pdf_filter = create_svg_to_pdf_filter(svg_to_pdf_location, output_base)

    # Create a line by line filter that processes both local file and remote url included graphics
    complete_url_filter = filter_list_filter([relative_path_filter] + download_filters + [svg_to_pdf_filter])
    image_line_filter = create_filter_applicator(complete_url_filter, extract_image_url_sequence_from_tex)

    # Add a line by line filter to fix up graphics scaling
    line_filter = filter_list_filter([image_line_filter, graphics_width_line_filter])

    return line_filter


def create_svg_to_pdf_filter(download_to, output_base):
    def svg_to_pdf_filter(path):
        if image_is_svg(path):
            source_path = os.path.join(output_base, path)
            _, leaf_name = os.path.split(source_path)
            pdf_leaf_name = leaf_name[0:-3] + 'pdf'
            destination_path = os.path.join(download_to, pdf_leaf_name)
            svg_to_pdf(source_path, destination_path)
            return determine_relative_path(destination_path, output_base)
        else:
            return path

    return svg_to_pdf_filter


def svg_to_pdf(svg_filename, pdf_filename):
    from svglib.svglib import svg2rlg
    from reportlab.graphics import renderPDF
    drawing = svg2rlg(svg_filename)
    renderPDF.drawToFile(drawing, pdf_filename)


def graphics_width_line_filter(source_line):
    # Insert default width scaling any place scaling is missing.
    return source_line.replace(r'\includegraphics{', r'\includegraphics[width=0.95\textwidth]{')
