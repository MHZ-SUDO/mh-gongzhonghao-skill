"""Compose genuine editable Office Math (OMML) with python-docx.

This is a small structure builder, NOT a LaTeX parser. Callers choose the
mathematical meaning and verify the equations. Strings become ordinary math
runs; use sub/sup/fraction/integral for those actual structures.
"""
from __future__ import annotations

from copy import deepcopy
from numbers import Number

from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt
from lxml import etree

MATH_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
CONTENT_TAGS = {"r", "sSub", "sSup", "sSubSup", "f", "d", "nary", "eqArr"}


def _node(tag: str, **attributes):
    element = OxmlElement("m:" + tag)
    for key, value in attributes.items():
        element.set(qn("m:" + key), str(value))
    return element


def text(value, *, upright: bool = False):
    """A math run. Use upright=True for units, operators, and normal text."""
    if not isinstance(value, (str, Number)):
        raise TypeError("text accepts a string or number")
    run = _node("r")
    if upright:
        properties = _node("rPr")
        properties.append(_node("sty", val="p"))
        run.append(properties)
    properties = OxmlElement("w:rPr")
    fonts = OxmlElement("w:rFonts")
    for name in ("ascii", "hAnsi", "eastAsia", "cs"):
        fonts.set(qn("w:" + name), "Cambria Math")
    properties.append(fonts)
    run.append(properties)
    content = _node("t")
    content.set(qn("xml:space"), "preserve")
    content.text = str(value)
    run.append(content)
    return run


def _parts(value):
    if isinstance(value, etree._Element):
        tag = etree.QName(value)
        if tag.namespace != MATH_NS or tag.localname not in CONTENT_TAGS:
            raise TypeError("equation parts must be supported OMML content elements")
        return [deepcopy(value)]
    if isinstance(value, (str, Number)):
        return [text(value)]
    if isinstance(value, (list, tuple)):
        return [element for part in value for element in _parts(part)]
    raise TypeError("equation parts must be strings, numbers, OMML, lists or tuples")


def _expression(tag: str, parts):
    expression = _node(tag)
    expression.extend(_parts(parts))
    return expression


def sub(base, index):
    result = _node("sSub")
    result.extend((_expression("e", base), _expression("sub", index)))
    return result


def sup(base, exponent):
    result = _node("sSup")
    result.extend((_expression("e", base), _expression("sup", exponent)))
    return result


def subsup(base, index, exponent):
    result = _node("sSubSup")
    result.extend((_expression("e", base), _expression("sub", index),
                   _expression("sup", exponent)))
    return result


def fraction(numerator, denominator):
    result = _node("f")
    properties = _node("fPr")
    properties.append(_node("type", val="bar"))
    result.extend((properties, _expression("num", numerator),
                   _expression("den", denominator)))
    return result


def delimiter(content, *, begin: str = "(", end: str = ")"):
    """One growable pair of delimiters surrounding an expression."""
    result = _node("d")
    properties = _node("dPr")
    properties.extend((_node("begChr", val=begin), _node("endChr", val=end),
                       _node("grow", val="1")))
    result.extend((properties, _expression("e", content)))
    return result


def integral(lower, upper, body):
    """Definite integral. Include the differential, e.g. [f, 'd', 't'], in body."""
    result = _node("nary")
    properties = _node("naryPr")
    properties.extend((_node("chr", val="∫"), _node("limLoc", val="subSup"),
                       _node("grow", val="1"), _node("subHide", val="0"),
                       _node("supHide", val="0")))
    result.extend((properties, _expression("sub", lower),
                   _expression("sup", upper), _expression("e", body)))
    return result


def equation_array(rows):
    """A genuine multi-row equation array. Each row is an expression/list."""
    if not isinstance(rows, (list, tuple)) or not rows:
        raise ValueError("rows must be a nonempty list or tuple")
    result = _node("eqArr")
    properties = _node("eqArrPr")
    properties.append(_node("baseJc", val="center"))
    result.append(properties)
    for row in rows:
        result.append(_expression("e", row))
    return result


def insert_equation(container, parts, *, font_size: float = 13):
    """Append a centered editable equation to a Document or table cell.

    Input is validated before adding a paragraph. Elements are deep-copied so
    reusing a constructed term does not move it out of an earlier equation.
    Returns the python-docx Paragraph for further layout formatting.
    """
    if not callable(getattr(container, "add_paragraph", None)):
        raise TypeError("container must expose add_paragraph (Document or cell)")
    if not isinstance(font_size, Number) or not 6 <= font_size <= 72:
        raise ValueError("font_size must be between 6 and 72 points")
    contents = _parts(parts)
    if not contents:
        raise ValueError("equation must not be empty")
    half_points = str(round(float(font_size) * 2))
    for element in contents:
        for run in ([element] if element.tag == qn("m:r") else element.iter(qn("m:r"))):
            properties = run.find(qn("w:rPr"))
            if properties is None:
                properties = OxmlElement("w:rPr")
                run.append(properties)
            for previous in properties.findall(qn("w:sz")):
                properties.remove(previous)
            size = OxmlElement("w:sz")
            size.set(qn("w:val"), half_points)
            properties.append(size)
    paragraph = container.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.space_before = Pt(6)
    paragraph.paragraph_format.space_after = Pt(6)
    paragraph.paragraph_format.line_spacing = 1.15
    paragraph.paragraph_format.keep_together = True
    math_paragraph = _node("oMathPara")
    properties = _node("oMathParaPr")
    properties.append(_node("jc", val="center"))
    expression = _node("oMath")
    expression.extend(contents)
    math_paragraph.extend((properties, expression))
    paragraph._p.append(math_paragraph)
    return paragraph
